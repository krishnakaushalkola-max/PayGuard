"""PayPal Orders v2 integration (sandbox).

Flow: OAuth token -> create order -> buyer approves on PayPal -> capture order.
PAYPAL_MODE=mock simulates this locally for dry runs/tests only.
"""
import os
import time
import uuid

import requests

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_token_cache = {"token": None, "expires": 0}


class PayPalError(Exception):
    pass


def mode() -> str:
    return os.environ.get("PAYPAL_MODE", "sandbox").strip().lower()


def _usd(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _get_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 30:
        return _token_cache["token"]
    cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not cid or not secret:
        raise PayPalError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in .env")
    try:
        r = requests.post(f"{SANDBOX_BASE}/v1/oauth2/token", auth=(cid, secret),
                          data={"grant_type": "client_credentials"}, timeout=30)
    except requests.RequestException as exc:
        raise PayPalError(f"Could not reach PayPal: {exc}") from exc
    if r.status_code != 200:
        raise PayPalError(f"PayPal auth failed ({r.status_code}). Check your Sandbox Client ID and Secret.")
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + int(data.get("expires_in", 300))
    return _token_cache["token"]


def create_order(req_id: int, items: list, total_cents: int, return_url: str, cancel_url: str) -> dict:
    """Returns {"order_id": str, "approve_url": str}."""
    if mode() == "mock":
        oid = f"MOCK-{req_id}-{uuid.uuid4().hex[:8]}"
        return {"order_id": oid, "approve_url": f"/mock-paypal?token={oid}"}

    pu_items = [{"name": i["name"][:127],
                 "unit_amount": {"currency_code": "USD", "value": _usd(i["unit_cents"])},
                 "quantity": str(i["quantity"]), "category": "PHYSICAL_GOODS"} for i in items]
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": f"payguard-{req_id}",
            "custom_id": str(req_id),
            "description": f"PayGuard request #{req_id}",
            "amount": {"currency_code": "USD", "value": _usd(total_cents),
                       "breakdown": {"item_total": {"currency_code": "USD", "value": _usd(total_cents)}}},
            "items": pu_items,
        }],
        "payment_source": {"paypal": {"experience_context": {
            "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
            "brand_name": "PayGuard",
            "locale": "en-US",
            "landing_page": "LOGIN",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }}},
    }
    try:
        r = requests.post(f"{SANDBOX_BASE}/v2/checkout/orders", json=body, timeout=30,
                          headers={"Authorization": f"Bearer {_get_token()}",
                                   "Content-Type": "application/json",
                                   "PayPal-Request-Id": f"payguard-order-{req_id}"})
    except requests.RequestException as exc:
        raise PayPalError(f"Could not reach PayPal: {exc}") from exc
    if r.status_code not in (200, 201):
        raise PayPalError(f"PayPal create-order failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    url = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), "")
    if not url:
        raise PayPalError("PayPal did not return an approval link.")
    return {"order_id": data["id"], "approve_url": url}


def capture_order(order_id: str) -> dict:
    """Returns {"status": str, "capture_id": str}."""
    if mode() == "mock":
        return {"status": "COMPLETED", "capture_id": f"MOCKCAP-{order_id[-8:]}"}
    try:
        r = requests.post(f"{SANDBOX_BASE}/v2/checkout/orders/{order_id}/capture", json={}, timeout=30,
                          headers={"Authorization": f"Bearer {_get_token()}",
                                   "Content-Type": "application/json",
                                   "PayPal-Request-Id": f"payguard-capture-{order_id}"})
    except requests.RequestException as exc:
        raise PayPalError(f"Could not reach PayPal: {exc}") from exc
    if r.status_code not in (200, 201):
        raise PayPalError(f"PayPal capture failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    cap_id = ""
    try:
        cap_id = data["purchase_units"][0]["payments"]["captures"][0]["id"]
    except (KeyError, IndexError):
        pass
    return {"status": data.get("status", ""), "capture_id": cap_id}
