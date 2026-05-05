"""Apply category rules to parsed transactions and flag self-transfers.

Reads:
  data/transactions.json   raw parsed transactions
  config/categories.json   self-transfer names + category rules

Writes:
  data/categorized.json    same transactions + extra fields:
                             - category          string ("Groceries", "Rent", "Self Transfer", "Review")
                             - is_self_transfer  bool
                             - is_review_needed  bool

Then prints a summary so you can see what fell into "Review" and tighten rules.
"""

# Python 3.9 compatibility.
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_DIR = Path(__file__).parent.parent / "config"


def is_self_transfer(txn: dict, names: list[str]) -> bool:
    """A txn is a self-transfer when the counterparty includes one of the configured names.

    HDFC's IB FUNDS TRANSFER messages always include the human name of the
    other side (e.g. "IB FUNDS TRANSFER CR-XXXXXXXXXX1234-FIRST LASTNAME"),
    so a substring match on a configured name is enough.
    """
    counterparty = (txn.get("counterparty") or "").upper()
    return any(name.upper() in counterparty for name in names)


def find_category(txn: dict, rules: list[dict]) -> str | None:
    """Return the first category whose `match` substring appears in the counterparty.

    Match is case-insensitive. Rules are evaluated in file order — put more
    specific rules above more general ones in categories.json.
    """
    counterparty = (txn.get("counterparty") or "").upper()
    for rule in rules:
        if rule["match"].upper() in counterparty:
            return rule["category"]
    return None


def categorize_all(txns: list[dict], config: dict) -> list[dict]:
    """Annotate every transaction in `txns` with category + flags. Returns the same list."""
    names = config["self_transfer_names"]
    rules = config["rules"]

    for t in txns:
        if is_self_transfer(t, names):
            t["category"] = "Self Transfer"
            t["is_self_transfer"] = True
            t["is_review_needed"] = False
        else:
            cat = find_category(t, rules)
            t["category"] = cat or "Review"
            t["is_self_transfer"] = False
            t["is_review_needed"] = cat is None
    return txns


# ---- runnable demo ----

def main() -> None:
    """CLI helper: re-runs categorization against the latest rules + user config
    and prints a summary. The dashboard does this live on every page load, so
    you don't need to run this — it's here for debugging.
    """
    txns = json.loads((DATA_DIR / "transactions.json").read_text())
    cat_config = json.loads((CONFIG_DIR / "categories.json").read_text())
    user_config = json.loads((CONFIG_DIR / "user.json").read_text())

    merged = {
        "rules": cat_config.get("rules", []),
        "self_transfer_names": user_config.get("self_transfer_names")
            or cat_config.get("self_transfer_names", []),
    }
    categorize_all(txns, merged)

    out_path = DATA_DIR / "categorized.json"
    out_path.write_text(json.dumps(txns, indent=2))
    print(f"Categorized {len(txns)} transactions → {out_path}\n")

    # Totals per category, debits only
    cat_totals: dict[str, float] = defaultdict(float)
    cat_counts: Counter[str] = Counter()
    for t in txns:
        if t["type"] == "debit":
            cat_totals[t["category"]] += t["amount"]
            cat_counts[t["category"]] += 1

    print("Debits by category:")
    for cat in sorted(cat_totals, key=lambda c: -cat_totals[c]):
        print(f"  {cat:18s} {cat_counts[cat]:>4d} txns  Rs.{cat_totals[cat]:>12,.2f}")

    # Budget account view (read from user.json)
    joint_acct = user_config["joint_account"]
    budget = user_config["monthly_budget"]
    joint = [t for t in txns if t["account"] == joint_acct]
    real_spend = sum(t["amount"] for t in joint
                     if t["type"] == "debit" and not t["is_self_transfer"])
    self_xfers = sum(t["amount"] for t in joint
                     if t["type"] == "debit" and t["is_self_transfer"])
    review_count = sum(1 for t in joint if t["is_review_needed"])

    pct = real_spend / budget * 100 if budget else 0

    print(f"\n--- Account {joint_acct} ---")
    print(f"  Real spending:    Rs.{real_spend:>12,.2f}  ({pct:.0f}% of Rs.{budget:,} budget)")
    print(f"  Self-transfers:   Rs.{self_xfers:>12,.2f}  (excluded)")
    print(f"  Review needed:    {review_count} transactions")

    # Top "Review" items so we can grow the rules file
    review_items = [t for t in txns
                    if t["category"] == "Review" and t["type"] == "debit"]
    review_items.sort(key=lambda t: -t["amount"])
    print(f"\nTop 10 uncategorized debits (add to categories.json to clear them):")
    for t in review_items[:10]:
        print(f"  Rs.{t['amount']:>10,.2f}  [{t['account']}]  {t['counterparty']}")


if __name__ == "__main__":
    main()
