"""First-run setup wizard for Saviour.

Run once to create config/user.json and .env, then test the Gmail connection
and (optionally) pull a first batch of transactions. After this, just run:

    python3 src/app.py

Then open http://localhost:5001 in your browser.
"""

# Python 3.9 compatibility.
from __future__ import annotations

import getpass
import imaplib
import json
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
ENV_PATH = PROJECT_ROOT / ".env"
USER_PATH = CONFIG_DIR / "user.json"


# ---- pretty printing ----

def header(text: str) -> None:
    print()
    print("─" * 60)
    print(text)
    print("─" * 60)


def info(text: str) -> None:
    print(text)


def ok(text: str) -> None:
    print(f"  ✓ {text}")


def err(text: str) -> None:
    print(f"  ✗ {text}")


# ---- prompts ----

def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  (required)")


def prompt_password(label: str) -> str:
    """Hide typed input, then strip spaces (Google shows the app password
    grouped as 4 chars + space + 4 chars, but spaces aren't part of it)."""
    while True:
        value = getpass.getpass(f"{label} (input hidden): ").strip()
        if value:
            return value.replace(" ", "")
        print("  (required)")


def prompt_list(label: str, instructions: str) -> list[str]:
    """Read multiple lines until a blank one is entered."""
    print(f"\n{label}")
    print(f"  {instructions}")
    print("  Enter one per line. Blank line to finish.")
    items: list[str] = []
    while True:
        v = input("  > ").strip()
        if not v:
            if items:
                return items
            print("  (need at least one)")
            continue
        items.append(v)


def prompt_int(label: str) -> int:
    while True:
        v = input(f"{label}: ").strip().replace(",", "").replace("₹", "")
        try:
            return int(v)
        except ValueError:
            print("  (numbers only, e.g. 200000)")


# ---- IMAP test ----

def test_gmail(email: str, password: str) -> bool:
    """Open an SSL connection to Gmail's IMAP, log in, and immediately log out.
    Returns True on success.
    """
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        conn.login(email, password)
        conn.select("INBOX")
        conn.logout()
        return True
    except imaplib.IMAP4.error as e:
        err(f"Gmail rejected the credentials: {e}")
        return False
    except Exception as e:
        err(f"Connection failed: {e}")
        return False


# ---- main wizard ----

def main() -> None:
    header("Saviour — first-run setup")
    info(
        "This wizard creates two files:\n"
        f"   {ENV_PATH.relative_to(PROJECT_ROOT)}      (your Gmail credentials)\n"
        f"   {USER_PATH.relative_to(PROJECT_ROOT)}  (your name, accounts, budget)\n"
        "Both are local-only and listed in .gitignore. They never leave this Mac."
    )

    # --- Name ---
    header("Your name")
    name = prompt("First name (e.g. Alex)")

    # --- Gmail ---
    header("Gmail account")
    info(
        "Saviour reads HDFC alert emails from your Gmail to pull transactions.\n"
        "We'll use an *app password* (separate from your real Gmail password).\n"
        "It's 16 characters and tied just to Saviour. You can revoke it anytime.\n\n"
        "1. Open: https://myaccount.google.com/apppasswords\n"
        "2. (If 2-Step Verification isn't on yet, enable it first at\n"
        "    https://myaccount.google.com/security)\n"
        "3. Type 'Saviour' as the app name, click Create.\n"
        "4. Copy the 16-character password Google shows you.\n"
    )
    while True:
        gmail = prompt("Your Gmail address (e.g. you@gmail.com)")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", gmail):
            err("That doesn't look like a valid email.")
            continue
        password = prompt_password("App password")

        info("\nTesting Gmail connection...")
        if test_gmail(gmail, password):
            ok("connected.")
            break
        info("Try again, or Ctrl+C to abort.")

    # --- Accounts ---
    header("Your bank accounts")
    info(
        "Saviour matches transactions by the LAST 4 DIGITS of your account or card.\n"
        "Find these on any HDFC alert email — e.g., 'account 1234' or 'Card ending 5678'."
    )
    accounts = prompt_list(
        "Bank account suffixes (savings/current accounts):",
        "Just the last 4 digits.",
    )
    cards = []
    info("\nCredit card suffixes (optional, blank line to skip):")
    while True:
        v = input("  > ").strip()
        if not v:
            break
        cards.append(v)

    # --- Joint / primary account for budgeting ---
    header("Budget account")
    info("Saviour tracks one account against a monthly budget on the dashboard.")
    if len(accounts) == 1:
        joint = accounts[0]
        info(f"Using {joint} (your only bank account).")
    else:
        while True:
            joint = prompt(
                "Which account should be the budgeted one? "
                f"({', '.join(accounts)})"
            )
            if joint in accounts:
                break
            err(f"'{joint}' isn't in your list. Pick one of the above.")

    monthly_budget = prompt_int(f"Monthly budget for account {joint} (in ₹, e.g. 200000)")

    # --- Self-transfer names ---
    header("Self-transfer names")
    info(
        "When money moves between your own accounts, or between you and your\n"
        "spouse, HDFC includes the human name in the alert (e.g.\n"
        "'IB FUNDS TRANSFER CR-XXXXXXXXXX1234-FIRST LASTNAME'). Saviour uses\n"
        "the names you list here to recognize those as transfers, not spending."
    )
    names = prompt_list(
        "Names that count as 'you' (one per line):",
        "Use full names as they'd appear on a bank transfer (UPPERCASE OK).",
    )

    # --- Write config files ---
    header("Saving config")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    user_config = {
        "name": name,
        "account_suffixes": accounts,
        "card_suffixes": cards,
        "joint_account": joint,
        "monthly_budget": monthly_budget,
        "self_transfer_names": names,
    }
    USER_PATH.write_text(json.dumps(user_config, indent=2) + "\n")
    ok(f"wrote {USER_PATH.relative_to(PROJECT_ROOT)}")

    ENV_PATH.write_text(
        f"GMAIL_ADDRESS={gmail}\n"
        f"GMAIL_APP_PASSWORD={password}\n"
    )
    ENV_PATH.chmod(0o600)  # owner read/write only
    ok(f"wrote {ENV_PATH.relative_to(PROJECT_ROOT)} (permissions 0600)")

    # --- Optional first sync ---
    header("Initial sync")
    info(
        "Pull the last 90 days of HDFC alerts now? This may take ~30s for a\n"
        "first run (talks to Gmail, parses emails, saves transactions.json)."
    )
    do_sync = input("Run first sync now? [Y/n]: ").strip().lower()
    if do_sync in ("", "y", "yes"):
        info("\nFetching... (output below from gmail_fetcher.py)\n")
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "src" / "gmail_fetcher.py")],
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            err("Fetch hit a problem — you can retry later with: python3 src/gmail_fetcher.py")
        else:
            ok("synced.")
    else:
        info("Skipped. You can run sync any time from the dashboard's ⟳ Sync button.")

    # --- Done ---
    header("All set")
    info(
        "To open Saviour:\n"
        "    python3 src/app.py\n"
        "Then visit http://localhost:5001 in your browser.\n"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted. No files were written.")
        sys.exit(1)
