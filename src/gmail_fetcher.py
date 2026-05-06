"""Connect to Gmail over IMAP, find HDFC transaction emails, and parse them.

This is *incremental*: every run appends new transactions to
data/transactions.json and skips any email whose Message-ID is already saved.
You can run it as often as you like — duplicates won't be created.

Date window:
  SINCE / BEFORE default to "last 90 days through tomorrow", which covers
  late-arriving alerts. Override with env vars:
    SAVIOUR_SINCE=01-Mar-2026 SAVIOUR_BEFORE=01-Jun-2026 python3 src/gmail_fetcher.py
"""

# Python 3.9 compatibility (macOS Xcode CLI Tools ships 3.9).
from __future__ import annotations

import imaplib
import email
import json
import os
import re
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path

from dotenv import load_dotenv

from parser import parse_email


# ---- config ----

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993  # standard SSL port for IMAP

HDFC_SENDERS = ["alerts@hdfcbank.net", "alerts@hdfcbank.bank.in"]

# Default date window: last 90 days through tomorrow. IMAP wants "DD-Mon-YYYY".
_today = datetime.now()
SINCE = os.environ.get("SAVIOUR_SINCE",
                       (_today - timedelta(days=90)).strftime("%d-%b-%Y"))
BEFORE = os.environ.get("SAVIOUR_BEFORE",
                        (_today + timedelta(days=1)).strftime("%d-%b-%Y"))

# Phrases that mean "this email is HDFC admin noise, not a transaction".
# If a body matches one of these AND parser returns None, skip silently.
NON_TRANSACTION_PHRASES = (
    "OTP",
    "successfully reset your NetBanking password",
    "accepted the terms & conditions",
)


def is_non_transaction(subject: str, body: str) -> bool:
    haystack = f"{subject} {body}"
    return any(phrase in haystack for phrase in NON_TRANSACTION_PHRASES)


# ---- IMAP plumbing ----

def connect() -> imaplib.IMAP4_SSL:
    """Open an encrypted IMAP connection to Gmail and log in."""
    load_dotenv()  # reads .env into os.environ
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    conn.login(address, password)
    conn.select("INBOX")  # tell the server which mailbox to read from
    return conn


def search_hdfc(conn: imaplib.IMAP4_SSL) -> list[bytes]:
    """Return Gmail message IDs for HDFC alerts in our date window.

    IMAP search syntax is its own little language. We build:
        (OR FROM "alerts@hdfcbank.net" FROM "alerts@hdfcbank.bank.in")
        SINCE 01-Apr-2026 BEFORE 01-May-2026
    """
    from_clause = f'(OR FROM "{HDFC_SENDERS[0]}" FROM "{HDFC_SENDERS[1]}")'
    query = f'{from_clause} SINCE {SINCE} BEFORE {BEFORE}'

    status, data = conn.search(None, query)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")

    # `data` is a list with one bytes element of space-separated IDs, e.g. b"1 2 3"
    return data[0].split()


def fetch_message(conn: imaplib.IMAP4_SSL, msg_id: bytes) -> Message:
    """Download one full email by ID and return it as a parsed Message object."""
    status, data = conn.fetch(msg_id, "(RFC822)")  # RFC822 = full raw email
    if status != "OK":
        raise RuntimeError(f"IMAP fetch failed for {msg_id!r}")
    raw_bytes = data[0][1]
    return email.message_from_bytes(raw_bytes)


def fetch_message_ids_bulk(conn: imaplib.IMAP4_SSL, msg_ids: list[bytes]) -> dict[bytes, str]:
    """One IMAP request for ALL message IDs' headers at once. Network round-trip
    to Gmail is ~600ms — bulk avoids paying it N times.

    Returns {sequence_number_bytes: 'message-id-string'}.
    """
    if not msg_ids:
        return {}
    id_set = b",".join(msg_ids)
    status, data = conn.fetch(id_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    if status != "OK":
        return {}

    # IMAP fetch response is a flat list alternating between tuples (header, body)
    # and standalone close-paren bytes. Each tuple's header looks like
    # b'<seqnum> (BODY[HEADER.FIELDS ("MESSAGE-ID")] {N}', and the body holds the
    # actual header lines.
    out: dict[bytes, str] = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        header_bytes, body_bytes = item
        m = re.match(rb"(\d+)\s+\(", header_bytes)
        if not m:
            continue
        seq = m.group(1)
        body_text = body_bytes.decode(errors="replace") if isinstance(body_bytes, (bytes, bytearray)) else str(body_bytes)
        mid_m = re.search(r"Message-ID:\s*(<[^>]+>)", body_text, re.IGNORECASE)
        if mid_m:
            out[seq] = mid_m.group(1).strip()
    return out


# ---- body extraction ----

def get_text_body(msg: Message) -> str:
    """Pull the human-readable text out of an email.

    Emails are usually 'multipart' — the same content in plain text AND HTML.
    We prefer plain text. If only HTML exists, we strip the tags crudely.
    """
    plain = None
    html = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and plain is None:
                plain = part.get_payload(decode=True).decode(errors="replace")
            elif content_type == "text/html" and html is None:
                html = part.get_payload(decode=True).decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True).decode(errors="replace")
        if msg.get_content_type() == "text/html":
            html = payload
        else:
            plain = payload

    if plain:
        return plain
    if html:
        return _strip_html(html)
    return ""


def _strip_html(html: str) -> str:
    """Crude HTML→text. Remove tags, collapse whitespace. Good enough for alerts."""
    no_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", no_tags).strip()


# ---- main ----

DATA_DIR = Path(__file__).parent.parent / "data"


TRANSACTIONS_PATH = DATA_DIR / "transactions.json"


def load_existing() -> list[dict]:
    """Read previously-saved transactions, or return empty list if none."""
    if TRANSACTIONS_PATH.exists():
        return json.loads(TRANSACTIONS_PATH.read_text())
    return []


def main(since: str | None = None, before: str | None = None) -> dict:
    """Fetch HDFC emails, dedupe, append to transactions.json. Returns a stats
    dict so callers (e.g. the /sync route) can show what happened.

    Pass since/before in 'DD-Mon-YYYY' format to override the module-level
    defaults (the dashboard's /sync route uses this to keep the window narrow).
    """
    since = since or SINCE
    before = before or BEFORE

    # Ensure data/ exists before any write_text call below. Fresh installs
    # don't have it because data/ is gitignored.
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect()
    print(f"Logged in to {os.environ['GMAIL_ADDRESS']}")

    try:
        # Build IMAP search query inline so we can use the per-call dates
        from_clause = f'(OR FROM "{HDFC_SENDERS[0]}" FROM "{HDFC_SENDERS[1]}")'
        query = f'{from_clause} SINCE {since} BEFORE {before}'
        status, data = conn.search(None, query)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        total = len(ids)
        print(f"Found {total} HDFC alerts between {since} and {before}\n")

        if not ids:
            return {"new": 0, "skipped_duplicate": 0, "skipped_admin": 0, "unparsed": 0}

        # Incremental: load what we already have, build a set of seen Message-IDs
        existing = load_existing()
        seen_ids = {t["email_id"] for t in existing if t.get("email_id")}
        print(f"Already have {len(existing)} transactions on disk "
              f"({len(seen_ids)} unique Message-IDs); will skip duplicates.\n")

        new_transactions: list[dict] = []
        unparsed: list[tuple[str, str, str]] = []
        skipped_admin = 0
        skipped_duplicate = 0

        # Bulk-fetch all Message-IDs in ONE IMAP request — pays the round-trip once
        print("Fetching Message-IDs in bulk for dedup check...")
        msg_id_map = fetch_message_ids_bulk(conn, ids)

        # Filter to only the IDs we actually need to fetch in full
        new_ids = [mid for mid in ids if msg_id_map.get(mid, "") not in seen_ids]
        skipped_duplicate = total - len(new_ids)
        print(f"  {skipped_duplicate} already on disk, {len(new_ids)} new to fetch.\n")

        for i, msg_id in enumerate(new_ids, start=1):
            # Full fetch + parse for new ones only
            msg = fetch_message(conn, msg_id)
            email_id = msg_id_map.get(msg_id) or (msg.get("Message-ID") or "").strip()

            subject = msg.get("Subject", "(no subject)")
            body = get_text_body(msg)
            result = parse_email(body)

            if result is None:
                if is_non_transaction(subject, body):
                    skipped_admin += 1
                else:
                    unparsed.append((msg_id.decode(), subject, body))
            else:
                # NetBanking emails don't carry a date in the body — use email date
                if result.get("date") is None:
                    parsed_dt = email.utils.parsedate_to_datetime(msg.get("Date"))
                    result["date"] = parsed_dt.strftime("%Y-%m-%d")
                result["email_id"] = email_id
                new_transactions.append(result)
                if email_id:
                    seen_ids.add(email_id)  # protect against rare in-run dupes

            if i % 10 == 0 or i == len(new_ids):
                print(f"  Fetched {i}/{len(new_ids)} new messages")

        # Append new transactions and write
        all_transactions = existing + new_transactions
        TRANSACTIONS_PATH.write_text(json.dumps(all_transactions, indent=2))
        print(f"\nAdded {len(new_transactions)} new transactions "
              f"(total now {len(all_transactions)}) → {TRANSACTIONS_PATH}")

        # Save unparsed bodies so we can examine and improve the parser
        if unparsed:
            unparsed_dir = DATA_DIR / "unparsed"
            unparsed_dir.mkdir(parents=True, exist_ok=True)
            for msg_id, subject, body in unparsed:
                (unparsed_dir / f"{msg_id}.txt").write_text(
                    f"Subject: {subject}\n\n{body}"
                )
            print(f"Saved {len(unparsed)} unparsed bodies to {unparsed_dir}")

        # Summary of THIS run
        debits = [t for t in new_transactions if t["type"] == "debit"]
        credits = [t for t in new_transactions if t["type"] == "credit"]

        print(f"\n--- This run ---")
        print(f"  New parsed:        {len(new_transactions)}")
        print(f"  Skipped duplicate: {skipped_duplicate}")
        print(f"  Skipped admin:     {skipped_admin}")
        print(f"  Unparsed:          {len(unparsed)}")
        print(f"  New debits:        {len(debits)}  total Rs.{sum(t['amount'] for t in debits):>12,.2f}")
        print(f"  New credits:       {len(credits)}  total Rs.{sum(t['amount'] for t in credits):>12,.2f}")

        return {
            "new": len(new_transactions),
            "skipped_duplicate": skipped_duplicate,
            "skipped_admin": skipped_admin,
            "unparsed": len(unparsed),
        }

    finally:
        conn.logout()


if __name__ == "__main__":
    main()
