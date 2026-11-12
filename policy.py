"""PayGuard policy engine.

This is plain, deterministic code. The AI never decides whether a purchase is
allowed: it only helps turn text into catalog items. All money rules live here.
"""
import os
from dataclasses import dataclass, field

AUTO = "auto_approved"
REVIEW = "needs_approval"
REJECT = "rejected"


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@dataclass
class PolicyConfig:
    auto_limit_cents: int = 1500          # <= $15 -> approved automatically
    approval_limit_cents: int = 10000     # <= $100 -> human approval needed; above -> rejected
    daily_budget_cents: int = 15000       # total paid per UTC day
    max_items: int = 10                   # max total quantity in one request
    always_review: set = field(default_factory=lambda: {"gift_cards"})
    blocked: set = field(default_factory=lambda: {"gambling"})


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _set_env(name: str, default: set) -> set:
    raw = os.environ.get(name)
    if raw is None:
        return set(default)
    return {p.strip() for p in raw.split(",") if p.strip()}


def load_config() -> PolicyConfig:
    d = PolicyConfig()
    return PolicyConfig(
        auto_limit_cents=_int_env("AUTO_LIMIT_CENTS", d.auto_limit_cents),
        approval_limit_cents=_int_env("APPROVAL_LIMIT_CENTS", d.approval_limit_cents),
        daily_budget_cents=_int_env("DAILY_BUDGET_CENTS", d.daily_budget_cents),
        max_items=_int_env("MAX_ITEMS", d.max_items),
        always_review=_set_env("ALWAYS_REVIEW_CATEGORIES", d.always_review),
        blocked=_set_env("BLOCKED_CATEGORIES", d.blocked),
    )


def evaluate(items: list, spent_today_cents: int, cfg: PolicyConfig) -> dict:
    """items: list of dicts with sku, name, category, unit_cents, quantity.

    Returns {"decision": ..., "reasons": [...], "total_cents": int}.
    """
    total = sum(i["unit_cents"] * i["quantity"] for i in items)
    qty = sum(i["quantity"] for i in items)
    reasons = []

    if not items:
        return {"decision": REJECT, "total_cents": 0,
                "reasons": ["No purchasable catalog items were found in the request."]}

    blocked = sorted({i["category"] for i in items if i["category"] in cfg.blocked})
    if blocked:
        reasons.append(f"Blocked category: {', '.join(blocked)}.")
    if qty > cfg.max_items:
        reasons.append(f"Too many items ({qty}); the limit is {cfg.max_items} per request.")
    if total > cfg.approval_limit_cents:
        reasons.append(f"Total {money(total)} is above the {money(cfg.approval_limit_cents)} hard limit.")
    if spent_today_cents + total > cfg.daily_budget_cents:
        reasons.append(
            f"Would exceed the daily budget of {money(cfg.daily_budget_cents)} "
            f"({money(spent_today_cents)} already spent today).")
    if reasons:
        return {"decision": REJECT, "reasons": reasons, "total_cents": total}

    review_cats = sorted({i["category"] for i in items if i["category"] in cfg.always_review})
    if review_cats:
        return {"decision": REVIEW, "total_cents": total,
                "reasons": [f"Category always needs human approval: {', '.join(review_cats)}."]}

    if total <= cfg.auto_limit_cents:
        return {"decision": AUTO, "total_cents": total,
                "reasons": [f"Total {money(total)} is within the {money(cfg.auto_limit_cents)} auto-approve limit."]}

    return {"decision": REVIEW, "total_cents": total,
            "reasons": [f"Total {money(total)} is above the {money(cfg.auto_limit_cents)} auto-approve limit, "
                        f"so a human must approve."]}
