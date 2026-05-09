"""Flask web dashboard for Saviour.

Run:    python3 src/app.py
Open:   http://localhost:5001 in your browser

Reads:
  data/categorized.json    rule-based categorization output
  data/overrides.json      manual per-transaction overrides (created by this app)
  config/categories.json   category list (used to populate the dropdown)
"""

# Python 3.9 compatibility.
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for

from categorizer import categorize_all
from gmail_fetcher import main as run_fetcher


app = Flask(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "categories.json"
USER_PATH = PROJECT_ROOT / "config" / "user.json"
OVERRIDES_PATH = DATA_DIR / "overrides.json"

# Make sure data/ exists at app startup. It's gitignored, so a fresh install
# has no folder. Doing it here means save_overrides() and the first sync work.
DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_user() -> dict:
    """Per-user config: name, accounts, budget, self-transfer names.
    All the things that should NOT be hardcoded if Saviour is to work for
    different people on different machines.
    """
    return json.loads(USER_PATH.read_text())


# ---- formatting ----

def format_inr(amount: float) -> str:
    """Format a number as Indian rupees, e.g. 123456.78 -> '₹1,23,456.78'."""
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    integer = int(amount)
    paise = round((amount - integer) * 100)

    s = str(integer)
    if len(s) <= 3:
        formatted = s
    else:
        last3, rest = s[-3:], s[:-3]
        chunks = []
        while len(rest) > 2:
            chunks.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            chunks.append(rest)
        chunks.reverse()
        formatted = ",".join(chunks) + "," + last3

    return f"{sign}₹{formatted}.{paise:02d}"


app.jinja_env.filters["inr"] = format_inr


def category_color(cat: str) -> str:
    """Deterministic hue for a category pill. Emits a CSS custom property
    only — the actual saturation/lightness is set by stylesheet rules so
    the same pill looks right in both light and dark mode.
    """
    h = int(hashlib.md5(cat.encode("utf-8")).hexdigest()[:4], 16) % 360
    return f"--cat-hue: {h};"


app.jinja_env.filters["catcolor"] = category_color


# ---- overrides ----

def make_keys(txns: list[dict]) -> None:
    """Annotate each transaction with a stable `_key` string we can target via UI.

    A key is `date|amount|account|counterparty|seq` where `seq` disambiguates
    same-day duplicates (e.g., two ₹500 Swiggy orders).
    """
    seen: dict[str, int] = {}
    for t in txns:
        base = f"{t['date']}|{t['amount']:.2f}|{t['account']}|{t['counterparty']}"
        n = seen.get(base, 0)
        t["_key"] = f"{base}|{n}"
        seen[base] = n + 1


def load_overrides() -> dict:
    if OVERRIDES_PATH.exists():
        return json.loads(OVERRIDES_PATH.read_text())
    return {}


def save_overrides(overrides: dict) -> None:
    OVERRIDES_PATH.write_text(json.dumps(overrides, indent=2))


def apply_overrides(txns: list[dict], overrides: dict) -> None:
    """For each transaction with an override, mutate its category / flags accordingly."""
    for t in txns:
        ov = overrides.get(t["_key"])
        if not ov:
            t["is_rejected"] = False
            continue
        if ov.get("rejected"):
            t["is_rejected"] = True
            t["category"] = "Excluded"
            t["is_review_needed"] = False
        elif ov.get("category"):
            t["category"] = ov["category"]
            t["is_review_needed"] = False
            t["is_rejected"] = False


def available_categories() -> list[str]:
    """All categories known to the system: from rules + any new ones the user
    typed into the dashboard (so 'Donations' appears in autocomplete the next
    time once you've used it once)."""
    config = json.loads(CONFIG_PATH.read_text())
    cats = {rule["category"] for rule in config["rules"]}
    for ov in load_overrides().values():
        if ov.get("category"):
            cats.add(ov["category"])
    return sorted(cats)


# ---- rule promotion ----

def _add_rules(counterparty_to_category: dict[str, str]) -> int:
    """Append rules to categories.json for any (counterparty, category) pairs
    not already represented. Returns the number of NEW rules added.

    Match key is lowercased counterparty — once 'Blinkit' has any rule,
    'BLINKIT' or 'blinkit' won't be added a second time.
    """
    if not counterparty_to_category:
        return 0
    config = json.loads(CONFIG_PATH.read_text())
    existing = {rule["match"].lower() for rule in config["rules"]}
    added = 0
    for cp, category in counterparty_to_category.items():
        if cp.lower() in existing:
            continue
        config["rules"].append({"match": cp, "category": category})
        existing.add(cp.lower())
        added += 1
    if added:
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
    return added


# ---- matched-pair detection ----

def find_matched_pairs(
    txns: list[dict],
    self_transfer_names: list[str],
    card_suffixes: list[str],
    joint_account: str,
) -> list[dict]:
    """Pair each credit-card debit with a same-amount inbound self-transfer to
    the joint account.

    Match criteria (all must hold):
      - CC debit on one of the user's card_suffixes
      - Inbound credit to joint_account already flagged as self-transfer
        (counterparty contains a self_transfer_name — i.e., user or spouse
        funded the reimbursement)
      - Within ±3 days of the CC date
      - Amount within ₹100 of the CC amount

    Greedy: each transfer can only be used once. Closest match (by amount-diff
    + day-distance) wins. Returns a list of {cc, transfer, from_name} dicts.
    """
    card_set = set(card_suffixes)
    cc_spends = sorted(
        (t for t in txns if t["account"] in card_set and t["type"] == "debit"),
        key=lambda t: t["date"],
    )
    transfers = [
        t for t in txns
        if t["account"] == joint_account
        and t["type"] == "credit"
        and t.get("is_self_transfer")
    ]

    used: set[str] = set()
    pairs: list[dict] = []
    for cc in cc_spends:
        cc_d = datetime.strptime(cc["date"], "%Y-%m-%d")
        cc_amt = cc["amount"]
        best, best_score = None, None
        for tr in transfers:
            if tr["_key"] in used:
                continue
            tr_d = datetime.strptime(tr["date"], "%Y-%m-%d")
            day_diff = (tr_d - cc_d).days  # signed: negative if transfer is BEFORE cc
            if abs(day_diff) > 3:
                continue
            amt_diff = abs(cc_amt - tr["amount"])
            if amt_diff > 100:
                continue
            # Score: amount match is the primary signal. Day distance is
            # secondary. Mild penalty for transfers BEFORE the CC date — the
            # typical pattern is spend-then-compensate, so a same-day or
            # after-CC transfer should beat a before-CC transfer of equal
            # amount distance.
            score = amt_diff + abs(day_diff) * 10
            if day_diff < 0:
                score += 5
            if best is None or score < best_score:
                best, best_score = tr, score
        if best is None:
            continue
        used.add(best["_key"])
        # Identify which person funded the transfer for display
        cp_upper = (best.get("counterparty") or "").upper()
        from_name = next(
            (name.split()[0] for name in self_transfer_names if name.upper() in cp_upper),
            "?",
        )
        pairs.append({"cc": cc, "transfer": best, "from_name": from_name})
    return pairs


def migrate_overrides_to_rules(txns: list[dict]) -> int:
    """Promote any per-transaction CATEGORY overrides into rules so they apply
    to all future matching transactions. Idempotent — only adds rules that
    don't exist yet.

    Skipped:
      - Rejections (they're per-txn by design)
      - Overrides marked `scope: "row"` (the user explicitly said "just this
        one transaction" via the drilldown Move button)
    """
    overrides = load_overrides()
    if not overrides:
        return 0
    key_to_cp = {t["_key"]: t["counterparty"] for t in txns}
    pairs: dict[str, str] = {}
    for key, ov in overrides.items():
        if ov.get("scope") == "row":
            continue  # explicit per-row override — don't promote
        cat = ov.get("category")
        if not cat:
            continue
        cp = key_to_cp.get(key)
        if cp and cp not in pairs:
            pairs[cp] = cat  # first seen wins on duplicates
    return _add_rules(pairs)


# ---- self-update from GitHub ----

# Cached state so we don't run `git fetch` on every request. Refreshed in the
# background once an hour. The first dashboard load after Flask starts may not
# show a banner even if updates exist — the next load (after the bg fetch
# finishes, ~1-2s later) will.
_UPDATE_STATE = {
    "available": False,
    "count": 0,
    "latest_message": None,
    "checked_at": 0,
    "checking": False,
    "error": None,
}
_UPDATE_LOCK = threading.Lock()
_UPDATE_TTL_SECONDS = 3600  # 1 hour


def _run_git(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a git subcommand in the project root. Raises on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def _do_update_check() -> None:
    """Fetch from origin and compute how many commits ahead origin/main is.
    Writes to _UPDATE_STATE. Safe to run in a background thread.
    """
    try:
        _run_git(["fetch", "origin", "main", "--quiet"], timeout=15)
        ahead = _run_git(["rev-list", "HEAD..origin/main", "--count"]).stdout.strip()
        count = int(ahead or "0")
        latest_msg = None
        if count > 0:
            latest_msg = _run_git(["log", "origin/main", "-1", "--pretty=%s"]).stdout.strip()
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                "available": count > 0,
                "count": count,
                "latest_message": latest_msg,
                "checked_at": time.time(),
                "checking": False,
                "error": None,
            })
    except Exception as e:
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                "checked_at": time.time(),
                "checking": False,
                "error": str(e)[:200],
            })


def maybe_check_for_updates() -> None:
    """Kick off a background update check if the cache is stale. Non-blocking."""
    now = time.time()
    with _UPDATE_LOCK:
        if _UPDATE_STATE["checking"]:
            return
        if now - _UPDATE_STATE["checked_at"] < _UPDATE_TTL_SECONDS:
            return
        _UPDATE_STATE["checking"] = True
    threading.Thread(target=_do_update_check, daemon=True).start()


# Kick off one check at import time so the banner appears on first page load
# (when possible — depending on network, may take 1-2s after Flask starts)
threading.Thread(target=_do_update_check, daemon=True).start()
_UPDATE_STATE["checking"] = True


# ---- routes ----

@app.route("/")
def dashboard():
    # Per-user settings — drives the budget number, which account is "joint",
    # the user's name in the header, and self-transfer name matching.
    user = load_user()
    joint_account = user["joint_account"]
    monthly_budget = user["monthly_budget"]
    card_suffixes = user.get("card_suffixes", [])
    self_transfer_names = user.get("self_transfer_names", [])

    # Load + categorize live (so edits to rules / overrides take effect on refresh).
    # On a brand-new install, transactions.json doesn't exist yet — render an
    # empty dashboard so the user can see the page and click ⟳ Sync from there.
    txns_path = DATA_DIR / "transactions.json"
    txns = json.loads(txns_path.read_text()) if txns_path.exists() else []
    make_keys(txns)
    migrate_overrides_to_rules(txns)

    # Merge categorizer config: rules from categories.json, names from user.json
    cat_config = json.loads(CONFIG_PATH.read_text())
    merged_for_categorize = {
        "rules": cat_config.get("rules", []),
        "self_transfer_names": self_transfer_names
            or cat_config.get("self_transfer_names", []),
    }
    categorize_all(txns, merged_for_categorize)
    apply_overrides(txns, load_overrides())

    # ---- Multi-month: figure out which month to show ----
    available_months = sorted(
        {t["date"][:7] for t in txns if t.get("date")},
        reverse=True,
    )
    selected_month = request.args.get("month")
    if selected_month not in available_months:
        # Default to the most recent month with data, or current month if empty
        selected_month = available_months[0] if available_months else datetime.now().strftime("%Y-%m")

    # Pretty labels for the dropdown
    month_options = [
        (m, datetime.strptime(m, "%Y-%m").strftime("%B %Y"))
        for m in available_months
    ]
    month_label = datetime.strptime(selected_month, "%Y-%m").strftime("%B %Y")

    # ---- Matched pairs (compute across all data, then filter to selected month) ----
    matched_pairs_all = find_matched_pairs(
        txns, self_transfer_names, card_suffixes, joint_account,
    )
    matched_pairs = [
        p for p in matched_pairs_all
        if p["cc"]["date"].startswith(selected_month)
        or p["transfer"]["date"].startswith(selected_month)
    ]

    # ---- Filter to the selected month for everything below ----
    txns_month = [t for t in txns if t.get("date", "").startswith(selected_month)]

    # Real "joint spending" = debits on the joint bank account PLUS debits on
    # any of the user's credit cards. CC spends ARE real spending the moment
    # they happen — waiting for the bill to land would lag the dashboard by
    # up to a month. The matched-pair set captures CC spends that were
    # personally compensated by an inbound transfer from spouse/own-account
    # (the "I paid for shoes on the card to earn points but it's my money"
    # case) — those are excluded so they don't bloat the joint number.
    card_set = set(card_suffixes)
    matched_cc_keys = {p["cc"]["_key"] for p in matched_pairs_all}

    joint = [t for t in txns_month
             if t["account"] == joint_account or t["account"] in card_set]
    real_spend = [t for t in joint
                  if t["type"] == "debit"
                  and not t.get("is_self_transfer")
                  and not t.get("is_rejected")
                  and t["_key"] not in matched_cc_keys]

    # Sync feedback banner (set when redirected from /sync)
    synced_new = request.args.get("synced_new")
    synced_dup = request.args.get("synced_dup")
    sync_error = request.args.get("sync_error")

    # Update banners (set when redirected from /update or /undo-update)
    just_updated = request.args.get("updated")
    just_reverted = request.args.get("reverted")
    update_error = request.args.get("update_error")

    # Recategorize toast (set when redirected from /recategorize-one)
    recat_to = request.args.get("recat_to")
    recat_key = request.args.get("recat_key")

    # Trigger a background check (non-blocking) so the "update available"
    # banner shows up on next request if applicable
    maybe_check_for_updates()
    update_status = dict(_UPDATE_STATE)  # snapshot so template doesn't see mid-write

    total = sum(t["amount"] for t in real_spend)
    pct = total / monthly_budget * 100 if monthly_budget else 0
    remaining = monthly_budget - total

    cat_totals: dict[str, float] = defaultdict(float)
    cat_counts: dict[str, int] = defaultdict(int)
    cat_txns: dict[str, list] = defaultdict(list)
    for t in real_spend:
        cat_totals[t["category"]] += t["amount"]
        cat_counts[t["category"]] += 1
        cat_txns[t["category"]].append(t)

    # Sort each category's drilldown by amount, biggest first
    for c in cat_txns:
        cat_txns[c].sort(key=lambda t: -t["amount"])

    # Build (category, amount, count, transactions) tuples for the template
    categories = sorted(
        [(c, cat_totals[c], cat_counts[c], cat_txns[c]) for c in cat_totals],
        key=lambda x: -x[1],
    )

    top = sorted(real_spend, key=lambda t: -t["amount"])[:10]

    review = sorted(
        [t for t in real_spend if t["is_review_needed"]],
        key=lambda t: -t["amount"],
    )

    rejected = sorted(
        [t for t in joint if t["is_rejected"]],
        key=lambda t: -t["amount"],
    )

    # ---- Monthly trend series (across ALL data, not just selected month) ----
    # Same definition as the hero number: joint-account + card debits,
    # excluding self-transfers, rejections, and matched-pair compensated CC
    # spends. JS renders the SVG client-side so we ship raw numbers and let
    # the user toggle ranges without a refresh.
    monthly_totals: dict[str, float] = defaultdict(float)
    for t in txns:
        if (
            t.get("type") == "debit"
            and not t.get("is_rejected")
            and t["_key"] not in matched_cc_keys
            and (
                (t.get("account") == joint_account and not t.get("is_self_transfer"))
                or t.get("account") in card_set
            )
        ):
            ym = (t.get("date") or "")[:7]
            if ym:
                monthly_totals[ym] += t["amount"]
    trend_series = [
        {
            "month": ym,
            "label": datetime.strptime(ym, "%Y-%m").strftime("%b %y"),
            "total": round(monthly_totals[ym], 2),
        }
        for ym in sorted(monthly_totals.keys())
    ]

    return render_template(
        "dashboard.html",
        user_name=user["name"],
        joint_account=joint_account,
        card_suffixes=card_suffixes,
        total=total,
        pct=pct,
        budget=monthly_budget,
        remaining=remaining,
        categories=categories,
        top=top,
        review=review,
        rejected=rejected,
        n_txns=len(real_spend),
        month_label=month_label,
        selected_month=selected_month,
        month_options=month_options,
        matched_pairs=matched_pairs,
        available_categories=available_categories(),
        # Sync banner
        synced_new=synced_new,
        synced_dup=synced_dup,
        sync_error=sync_error,
        # Update banners
        update_status=update_status,
        just_updated=just_updated,
        just_reverted=just_reverted,
        update_error=update_error,
        # Recategorize toast
        recat_to=recat_to,
        recat_key=recat_key,
        # Monthly trend chart
        trend_series=trend_series,
    )


@app.route("/update", methods=["POST"])
def update_app():
    """Pull latest code from GitHub + reinstall dependencies. Flask is in
    debug mode and watches Python files, so it auto-reloads itself when the
    pulled files land. The user just refreshes their browser after.
    """
    try:
        pull = _run_git(["pull", "origin", "main"], timeout=60)
        # Re-install dependencies in case requirements.txt changed
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"],
            cwd=str(PROJECT_ROOT),
            check=True,
            timeout=180,
        )
        # Bust the update cache so the banner clears immediately
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({
                "available": False,
                "count": 0,
                "latest_message": None,
                "checked_at": time.time(),
            })
        return redirect(url_for("dashboard", updated="1"))
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or str(e))[:300]
        return redirect(url_for("dashboard", update_error=msg))
    except Exception as e:
        return redirect(url_for("dashboard", update_error=str(e)[:300]))


@app.route("/undo-update", methods=["POST"])
def undo_update():
    """Roll back to the previous commit (last entry in reflog before now).
    Useful if a freshly-pulled update broke something.
    """
    try:
        _run_git(["reset", "--hard", "HEAD@{1}"], timeout=10)
        with _UPDATE_LOCK:
            _UPDATE_STATE["checked_at"] = 0  # re-check on next load
        return redirect(url_for("dashboard", reverted="1"))
    except Exception as e:
        return redirect(url_for("dashboard", update_error=str(e)[:300]))


@app.route("/sync-historical", methods=["POST"])
def sync_historical():
    """Fetch all HDFC alerts from a specific calendar month — for backfilling
    history. Dedup applies (the fetcher won't re-add transactions whose
    Message-IDs are already on disk), so this is safe to run on months that
    already have partial data.

    After a successful fetch, the dashboard auto-switches to that month so the
    user sees the freshly-loaded data right away.
    """
    target_month = request.form.get("month", "").strip()  # "YYYY-MM"
    from_month = request.form.get("from_month")  # currently-viewed month

    def _err(msg: str):
        params = {"sync_error": msg[:200]}
        if from_month:
            params["month"] = from_month
        return redirect(url_for("dashboard", **params))

    # Validate format
    try:
        target_dt = datetime.strptime(target_month, "%Y-%m")
    except ValueError:
        return _err("Invalid month format")

    # No fetching the future
    if target_dt > datetime.now():
        return _err("That month is in the future")

    # IMAP date window: first day of month → first day of next month (exclusive)
    since = target_dt.strftime("%d-%b-%Y")
    if target_dt.month == 12:
        next_dt = target_dt.replace(year=target_dt.year + 1, month=1)
    else:
        next_dt = target_dt.replace(month=target_dt.month + 1)
    before = next_dt.strftime("%d-%b-%Y")

    try:
        stats = run_fetcher(since=since, before=before)
        params = {
            "synced_new": stats["new"],
            "synced_dup": stats["skipped_duplicate"],
            "month": target_month,  # auto-switch to the month they just fetched
        }
    except Exception as e:
        return _err(str(e))

    return redirect(url_for("dashboard", **params))


@app.route("/sync", methods=["POST"])
def sync():
    """Run the Gmail fetcher in-process. Computes a tight SINCE based on the
    latest transaction we already have, so subsequent syncs are quick.
    Browser waits ~5–10s for a normal incremental sync.
    """
    from_month = request.form.get("from_month")
    try:
        # Narrow the window: 7 days before our latest transaction. This catches
        # any backdated alerts without re-walking months of history.
        existing = json.loads((DATA_DIR / "transactions.json").read_text() or "[]")
        since_arg = None
        if existing:
            latest = max(t["date"] for t in existing if t.get("date"))
            since_dt = datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=7)
            since_arg = since_dt.strftime("%d-%b-%Y")

        stats = run_fetcher(since=since_arg)
        params = {"synced_new": stats["new"], "synced_dup": stats["skipped_duplicate"]}
    except Exception as e:
        params = {"sync_error": str(e)[:200]}
    if from_month:
        params["month"] = from_month
    return redirect(url_for("dashboard", **params))


@app.route("/rename-category", methods=["POST"])
def rename_category():
    """Rename a category everywhere it appears: all rules + all per-txn overrides.

    Trim whitespace and ignore no-ops. Empty new name = cancel. Keeping the old
    name = cancel. Otherwise it's a global find-and-replace on the category
    string in both files.
    """
    old = request.form.get("old", "").strip()
    new = request.form.get("new", "").strip()
    from_month = request.form.get("from_month")
    if not old or not new or old == new:
        return redirect(url_for("dashboard", month=from_month) if from_month else url_for("dashboard"))

    # 1. Update every rule whose category == old
    config = json.loads(CONFIG_PATH.read_text())
    for rule in config["rules"]:
        if rule["category"] == old:
            rule["category"] = new
    CONFIG_PATH.write_text(json.dumps(config, indent=2))

    # 2. Update any per-txn overrides that still pin to the old name
    overrides = load_overrides()
    for ov in overrides.values():
        if ov.get("category") == old:
            ov["category"] = new
    save_overrides(overrides)

    return redirect(url_for("dashboard", month=from_month) if from_month else url_for("dashboard"))


@app.route("/recategorize-one", methods=["POST"])
def recategorize_one():
    """Move a single transaction to a different category — per-row override.

    Different intent from /categorize set (which writes a rule that affects all
    transactions from a counterparty). This writes a per-txn override marked
    `scope: "row"` so the migration helper doesn't promote it into a rule.
    """
    key = request.form.get("key", "").strip()
    category = request.form.get("category", "").strip()
    from_month = request.form.get("from_month")

    if not key or not category:
        return redirect(url_for("dashboard", month=from_month) if from_month else url_for("dashboard"))

    overrides = load_overrides()
    overrides[key] = {"category": category, "rejected": False, "scope": "row"}
    save_overrides(overrides)

    params = {"recat_to": category, "recat_key": key}
    if from_month:
        params["month"] = from_month
    return redirect(url_for("dashboard", **params))


@app.route("/categorize", methods=["POST"])
def categorize_txn():
    """Apply Save / Reject / Undo to one or many transactions at once.

    The review/rejected forms send a list of `key` values (one per ticked
    checkbox). For per-row Undo there's just one key. Same handler for both.
    """
    keys = request.form.getlist("key")
    action = request.form["action"]
    from_month = request.form.get("from_month")

    if not keys:
        return redirect(url_for("dashboard", month=from_month) if from_month else url_for("dashboard"))

    overrides = load_overrides()

    if action == "reject":
        for key in keys:
            overrides[key] = {"rejected": True}
    elif action == "set":
        category = request.form.get("category", "").strip()
        if category:
            # Promote each ticked txn's counterparty into a rule so it sticks
            # for all past + future matching transactions. No per-txn override
            # is written — the rule is the single source of truth.
            txns = json.loads((DATA_DIR / "transactions.json").read_text())
            make_keys(txns)
            key_to_cp = {t["_key"]: t["counterparty"] for t in txns}
            pairs: dict[str, str] = {}
            for key in keys:
                cp = key_to_cp.get(key)
                if cp and cp not in pairs:
                    pairs[cp] = category
            _add_rules(pairs)
    elif action == "undo":
        for key in keys:
            overrides.pop(key, None)

    save_overrides(overrides)
    return redirect(url_for("dashboard", month=from_month) if from_month else url_for("dashboard"))


if __name__ == "__main__":
    app.run(debug=True, port=5001)
