"""Parse HDFC transaction email bodies into structured Transaction dicts.

Patterns handled (try `parse_email(body)` to dispatch across all of them):

  Outflows from bank account:
    parse_upi_debit                        UPI to a VPA (most common case)
    parse_upi_account_to_account_debit     UPI to a bank account number
    parse_account_debited_transfer         "is deducted ... and added to ..." (IB FUNDS TRANSFER DR)
    parse_netbanking_payment               NetBanking payment (no date in body — caller fills it)

  Outflows from credit card:
    parse_credit_card_debit                "Rs.X has been debited from your HDFC Bank Credit Card ..."
    parse_credit_card_thank_you            "Thank you for using HDFC Bank Card XX..."

  Inflows to bank account:
    parse_upi_credit                       Incoming UPI to a VPA
    parse_account_credited                 "has been successfully added to your account ending XX..."
"""

# Required so `dict | None` etc. work on Python 3.9 (what macOS Xcode CLI Tools
# ships). Makes all annotations lazy-evaluated strings — they don't need to be
# actual types at runtime.
from __future__ import annotations

import re
from datetime import datetime


# ---- outflows: bank account ----

def parse_upi_debit(body: str) -> dict | None:
    """UPI debit where the counterparty is a VPA."""
    pattern = re.compile(
        r"Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+has been debited from account\s+(\d{4,})"
        r"\s+to VPA\s+(\S+)"
        r"\s+(.+?)"
        r"\s+on\s+(\d{2}-\d{2}-\d{2})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, vpa, counterparty, date_str = match.groups()
    return _txn(
        date=_format_dmy(date_str),
        amount=amount_str,
        type="debit",
        account=account,
        counterparty=counterparty,
        vpa=vpa,
        source="upi_debit",
    )


def parse_upi_debit_v2(body: str) -> dict | None:
    """New 2026-05 HDFC UPI debit format. Same outflow as parse_upi_debit but
    with reworded body:
        "Rs.X is debited from your account ending NNNN towards VPA <handle>
         (<NAME>) on DD-MM-YY."
    """
    pattern = re.compile(
        r"Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+is debited from your account ending\s+(\d{4,})"
        r"\s+towards VPA\s+(\S+)"
        r"\s*\((.+?)\)"
        r"\s+on\s+(\d{2}-\d{2}-\d{2})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, vpa, counterparty, date_str = match.groups()
    return _txn(
        date=_format_dmy(date_str),
        amount=amount_str,
        type="debit",
        account=account,
        counterparty=counterparty,
        vpa=vpa,
        source="upi_debit_v2",
    )


def parse_upi_account_to_account_debit(body: str) -> dict | None:
    """UPI debit where the counterparty is another bank account (no VPA)."""
    pattern = re.compile(
        r"Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+has been debited from account\s+(\d{4,})"
        r"\s+to account\s+\*+(\d+)"
        r"\s+on\s+(\d{2}-\d{2}-\d{2})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, target, date_str = match.groups()
    return _txn(
        date=_format_dmy(date_str),
        amount=amount_str,
        type="debit",
        account=account,
        counterparty=f"account *{target}",
        source="upi_account_transfer",
    )


def parse_account_debited_transfer(body: str) -> dict | None:
    """Inter-account transfer out: 'is deducted from your account ... and added to ...'."""
    pattern = re.compile(
        r"Rs\.?\s*(?:INR\s+)?([\d,]+\.\d{1,2})"
        r"\s+is deducted from your account ending\s+XX(\d{4,})"
        r"\s+and added to\s+(.+?)\s+account"
        r"\s+on\s+(\d{2}-[A-Z]{3}-\d{4})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, target_desc, date_str = match.groups()
    return _txn(
        date=_format_dmy_caps(date_str),
        amount=amount_str,
        type="debit",
        account=account,
        counterparty=target_desc,
        source="account_transfer_out",
    )


def parse_netbanking_payment(body: str) -> dict | None:
    """NetBanking payment. Note: this email has no date in the body; caller fills it."""
    pattern = re.compile(
        r"NetBanking for payment of\s+Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+from\s+A/c\s+\*+(\d{4,})"
        r"\s+to\s+([A-Z0-9]+)",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, merchant = match.groups()
    return _txn(
        date=None,
        amount=amount_str,
        type="debit",
        account=account,
        counterparty=merchant,
        source="netbanking_payment",
    )


# ---- outflows: credit card ----

def parse_credit_card_debit(body: str) -> dict | None:
    """Credit-card debit (purchase or ATM withdrawal)."""
    pattern = re.compile(
        r"Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+(?:is|has been)\s+debited from your HDFC Bank Credit Card ending\s+(\d{4,})"
        r"\s+(?:towards|for an ATM withdrawal at)\s+(.+?)"
        r"\s+on\s+(\d{1,2}\s+\w{3},?\s+\d{4})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, card, merchant, date_str = match.groups()
    return _txn(
        date=_format_long(date_str),
        amount=amount_str,
        type="debit",
        account=card,
        counterparty=merchant,
        source="credit_card_debit",
    )


def parse_credit_card_thank_you(body: str) -> dict | None:
    """'Thank you for using HDFC Bank Card XX... for Rs. ... at MERCHANT on DD-MM-YYYY'."""
    pattern = re.compile(
        r"Thank you for using HDFC Bank Card XX(\d{4,})"
        r"\s+for\s+Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+at\s+(.+?)"
        r"\s+on\s+(\d{2}-\d{2}-\d{4})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    card, amount_str, merchant, date_str = match.groups()
    return _txn(
        date=_format_dmy_full(date_str),
        amount=amount_str,
        type="debit",
        account=card,
        counterparty=merchant,
        source="credit_card_thank_you",
    )


# ---- inflows ----

def parse_upi_credit(body: str) -> dict | None:
    """Incoming UPI credit to a bank account."""
    pattern = re.compile(
        r"Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+is successfully credited to your account\s+\**(\d{4,})"
        r"\s+by VPA\s+(\S+)"
        r"\s+(.+?)"
        r"\s+on\s+(\d{2}-\d{2}-\d{2})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, vpa, counterparty, date_str = match.groups()
    return _txn(
        date=_format_dmy(date_str),
        amount=amount_str,
        type="credit",
        account=account,
        counterparty=counterparty,
        vpa=vpa,
        source="upi_credit",
    )


def parse_account_credited(body: str) -> dict | None:
    """'Rs.INR X has been successfully added to your account ending XX... from <desc> on DD-MON-YYYY'."""
    pattern = re.compile(
        r"Rs\.?\s*(?:INR\s+)?([\d,]+\.\d{1,2})"
        r"\s+has been successfully added to your account ending XX(\d{4,})"
        r"\s+from\s+(.+?)"
        r"\s+on\s+(\d{2}-[A-Z]{3}-\d{4})",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, source_desc, date_str = match.groups()
    return _txn(
        date=_format_dmy_caps(date_str),
        amount=amount_str,
        type="credit",
        account=account,
        counterparty=source_desc,
        source="account_credit",
    )


def parse_account_credited_v2(body: str) -> dict | None:
    """New 2026-05 HDFC inbound-credit format. Different wording from
    parse_account_credited and uses structured fields:
        "Rs.X has been successfully credited to your HDFC Bank account
         ending in NNNN. Transaction Details:
            a. Date: DD-MM-YY
            b. Sender: <NAME> (VPA: <handle>)
            c. UPI Reference No.: ..."
    """
    pattern = re.compile(
        r"Rs\.?\s*([\d,]+\.\d{1,2})"
        r"\s+has been successfully credited to your HDFC Bank account ending in\s+(\d{4,})"
        r".*?a\.\s*Date:\s*(\d{2}-\d{2}-\d{2})"
        r".*?b\.\s*Sender:\s*(.+?)\s*\(VPA:\s*(\S+?)\)",
    )
    match = pattern.search(body)
    if match is None:
        return None
    amount_str, account, date_str, sender_name, vpa = match.groups()
    return _txn(
        date=_format_dmy(date_str),
        amount=amount_str,
        type="credit",
        account=account,
        counterparty=sender_name,
        vpa=vpa,
        source="account_credit_v2",
    )


# ---- dispatcher ----

def parse_email(body: str) -> dict | None:
    """Try each parser in turn. Return the first match, or None if nothing fits."""
    parsers = (
        parse_upi_debit,
        parse_upi_debit_v2,
        parse_upi_account_to_account_debit,
        parse_credit_card_debit,
        parse_credit_card_thank_you,
        parse_upi_credit,
        parse_account_credited,
        parse_account_credited_v2,
        parse_account_debited_transfer,
        parse_netbanking_payment,
    )
    for parser in parsers:
        result = parser(body)
        if result is not None:
            return result
    return None


# ---- helpers ----

def _txn(*, date, amount, type, account, counterparty, source, vpa=None) -> dict:
    """Build a Transaction dict in a consistent shape."""
    return {
        "date": date,
        "amount": float(str(amount).replace(",", "")),
        "type": type,
        "account": account,
        "counterparty": counterparty.strip() if isinstance(counterparty, str) else counterparty,
        "vpa": vpa,
        "source": source,
    }


def _format_dmy(s: str) -> str:
    """'08-04-26' -> '2026-04-08'"""
    return datetime.strptime(s, "%d-%m-%y").strftime("%Y-%m-%d")


def _format_dmy_full(s: str) -> str:
    """'08-04-2026' -> '2026-04-08'"""
    return datetime.strptime(s, "%d-%m-%Y").strftime("%Y-%m-%d")


def _format_dmy_caps(s: str) -> str:
    """'02-APR-2026' -> '2026-04-02'"""
    return datetime.strptime(s, "%d-%b-%Y").strftime("%Y-%m-%d")


def _format_long(s: str) -> str:
    """'02 Apr, 2026' or '02 Apr 2026' -> '2026-04-02'"""
    cleaned = s.replace(",", "")
    return datetime.strptime(cleaned, "%d %b %Y").strftime("%Y-%m-%d")


# ---- runnable demo ----

if __name__ == "__main__":
    import json
    from pathlib import Path

    samples_dir = Path(__file__).parent.parent / "data" / "samples"
    for sample_path in sorted(samples_dir.glob("*.txt")):
        body = sample_path.read_text()
        result = parse_email(body)
        print(f"\n--- {sample_path.name} ---")
        print("  (no match)" if result is None else json.dumps(result, indent=2))
