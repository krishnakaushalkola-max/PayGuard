"""PayGuard: a spending-safe AI shopping agent with PayPal sandbox payments.

AI proposes -> rules decide -> human (or ESP32 button) approves -> PayPal pays.
"""
import hashlib
import hmac
import html
import json
import os
import secrets

from flask import Flask, jsonify, redirect, render_template, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

import database as db
import llm_parser
import paypal_client
import policy

NONCE_TTL = 30  # seconds a hardware-approval nonce stays valid
HERE = os.path.dirname(os.path.abspath(__file__))


def load_catalog() -> list:
    with open(os.path.join(HERE, "catalog.json"), encoding="utf-8") as f:
        return json.load(f)


def device_id() -> str:
    return os.environ.get("DEVICE_ID", "payguard-esp32-01")


def device_secret() -> str:
    return os.environ.get("DEVICE_SECRET", "change-me-to-a-long-random-string")


def hardware_required() -> bool:
    return os.environ.get("REQUIRE_HARDWARE", "0") == "1"


def base_url() -> str:
    return os.environ.get("BASE_URL", "").rstrip("/") or request.url_root.rstrip("/")


def public_request(r: dict) -> dict:
    return {k: r[k] for k in ("id", "text", "items", "unmatched", "summary", "total_cents", "status",
                              "decision", "reasons", "parser", "approved_by", "paypal_order_id",
                              "approve_url", "capture_id", "created_at", "updated_at")}


def start_payment(req_id: int, actor: str) -> None:
    """Create the PayPal order for an approved request."""
    r = db.get_request(req_id)
    try:
        order = paypal_client.create_order(
            req_id, r["items"], r["total_cents"],
            return_url=f"{base_url()}/paypal/return", cancel_url=f"{base_url()}/paypal/cancel")
    except paypal_client.PayPalError as exc:
        db.update_request(req_id, status="failed")
        db.add_event(req_id, "system", "payment_error", str(exc))
        return
    db.update_request(req_id, status="awaiting_payment", paypal_order_id=order["order_id"],
                      approve_url=order["approve_url"])
    db.add_event(req_id, "system", "paypal_order_created", f"order {order['order_id']}")


def approve_request(req_id: int, actor: str) -> bool:
    """pending_approval -> approved (atomic), then create the PayPal order."""
    if not db.transition(req_id, "pending_approval", status="approved", approved_by=actor):
        return False
    db.add_event(req_id, actor, "approved", "Human approval received.")
    start_payment(req_id, actor)
    return True


def create_app() -> Flask:
    app = Flask(__name__)
    db.init_db()
    catalog = load_catalog()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "llm_provider": llm_parser.provider(), "paypal_mode": paypal_client.mode()})

    @app.get("/api/state")
    def state():
        cfg = policy.load_config()
        return jsonify({
            "requests": [public_request(r) for r in db.list_requests()],
            "events": db.list_events(),
            "spent_today_cents": db.spent_today_cents(),
            "config": {
                "auto_limit_cents": cfg.auto_limit_cents, "approval_limit_cents": cfg.approval_limit_cents,
                "daily_budget_cents": cfg.daily_budget_cents, "max_items": cfg.max_items,
                "always_review": sorted(cfg.always_review), "blocked": sorted(cfg.blocked),
                "llm_provider": llm_parser.provider(), "paypal_mode": paypal_client.mode(),
                "hardware_required": hardware_required(), "device_id": device_id(),
            },
            "catalog": [{"name": c["name"], "category": c["category"], "price_cents": c["price_cents"]}
                        for c in catalog],
        })

    @app.post("/api/requests")
    def new_request():
        data = request.get_json(silent=True) or {}
        text = str(data.get("text", "")).strip()
        if not text:
            return jsonify({"error": "Please type what the agent should buy."}), 400
        if len(text) > 500:
            return jsonify({"error": "Request too long (max 500 characters)."}), 400

        parsed = llm_parser.parse_request(text, catalog)
        result = policy.evaluate(parsed["items"], db.spent_today_cents(), policy.load_config())
        status = {policy.AUTO: "approved", policy.REVIEW: "pending_approval",
                  policy.REJECT: "rejected"}[result["decision"]]
        req_id = db.create_request(text, parsed["items"], parsed["unmatched"], parsed["summary"],
                                   result["total_cents"], status, result["decision"],
                                   result["reasons"], parsed["parser"])
        db.add_event(req_id, "ai", "parsed", f"{parsed['parser']}: {len(parsed['items'])} item type(s)"
                     + (f" | {parsed['error']}" if parsed["error"] else ""))
        db.add_event(req_id, "policy", result["decision"], " ".join(result["reasons"]))
        if result["decision"] == policy.AUTO:
            db.update_request(req_id, approved_by="policy (auto)")
            start_payment(req_id, "policy")
        return jsonify(public_request(db.get_request(req_id))), 201

    @app.post("/api/requests/<int:req_id>/approve")
    def web_approve(req_id):
        if hardware_required():
            return jsonify({"error": "Hardware approval is required: press the ESP32 button."}), 403
        if not approve_request(req_id, "human (web)"):
            return jsonify({"error": "This request is not waiting for approval."}), 409
        return jsonify(public_request(db.get_request(req_id)))

    @app.post("/api/requests/<int:req_id>/reject")
    def web_reject(req_id):
        if not db.transition(req_id, "pending_approval", status="rejected", approved_by="human (web)"):
            return jsonify({"error": "This request is not waiting for approval."}), 409
        db.add_event(req_id, "human (web)", "rejected", "Rejected by the user.")
        return jsonify(public_request(db.get_request(req_id)))

    # ---- ESP32 hardware approval -------------------------------------------------
    @app.post("/api/device/challenge")
    def device_challenge():
        data = request.get_json(silent=True) or {}
        if data.get("device_id") != device_id():
            return jsonify({"error": "Unknown device."}), 403
        pending = [r for r in db.list_requests(100) if r["status"] == "pending_approval"]
        if not pending:
            return jsonify({"pending": False})
        r = pending[-1]  # oldest waiting request
        nonce = secrets.token_hex(8)
        db.create_nonce(nonce, r["id"], NONCE_TTL)
        return jsonify({"pending": True, "request_id": r["id"], "total_cents": r["total_cents"],
                        "summary": ", ".join(f"{i['quantity']}x {i['name']}" for i in r["items"])[:120],
                        "nonce": nonce, "expires_in": NONCE_TTL})

    @app.post("/api/device/approve")
    def device_approve():
        data = request.get_json(silent=True) or {}
        if data.get("device_id") != device_id():
            return jsonify({"error": "Unknown device."}), 403
        try:
            req_id = int(data.get("request_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Bad request_id."}), 400
        nonce, sig = str(data.get("nonce", "")), str(data.get("signature", ""))
        r = db.get_request(req_id)
        if not r or r["status"] != "pending_approval":
            return jsonify({"error": "Request is not waiting for approval."}), 409
        msg = f"{req_id}:{nonce}:{r['total_cents']}".encode()
        expected = hmac.new(device_secret().encode(), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            db.add_event(req_id, "device", "signature_rejected", "Invalid device signature.")
            return jsonify({"error": "Invalid signature."}), 403
        if not db.consume_nonce(nonce, req_id):
            db.add_event(req_id, "device", "nonce_rejected", "Nonce expired, reused or wrong request.")
            return jsonify({"error": "Nonce expired or already used."}), 403
        if not approve_request(req_id, "ESP32 button"):
            return jsonify({"error": "Request is not waiting for approval."}), 409
        return jsonify({"ok": True})

    # ---- PayPal redirects ---------------------------------------------------------
    @app.get("/paypal/return")
    def paypal_return():
        order_id = request.args.get("token", "")
        r = db.get_request_by_order(order_id)
        if not r:
            return redirect("/?msg=unknown_order")
        if db.transition(r["id"], "awaiting_payment", status="capturing"):
            try:
                cap = paypal_client.capture_order(order_id)
            except paypal_client.PayPalError as exc:
                db.update_request(r["id"], status="failed")
                db.add_event(r["id"], "system", "capture_error", str(exc))
                return redirect(f"/?focus={r['id']}")
            if cap["status"] == "COMPLETED":
                db.update_request(r["id"], status="paid", capture_id=cap["capture_id"])
                db.add_event(r["id"], "paypal", "paid", f"Captured {cap['capture_id']}")
            else:
                db.update_request(r["id"], status="failed")
                db.add_event(r["id"], "paypal", "capture_not_completed", cap["status"])
        return redirect(f"/?focus={r['id']}")

    @app.get("/paypal/cancel")
    def paypal_cancel():
        r = db.get_request_by_order(request.args.get("token", ""))
        if r and db.transition(r["id"], "awaiting_payment", status="cancelled"):
            db.add_event(r["id"], "buyer", "cancelled", "Buyer cancelled on PayPal.")
        return redirect("/")

    @app.get("/mock-paypal")
    def mock_paypal():
        if paypal_client.mode() != "mock":
            return "Mock PayPal is disabled.", 404
        token = html.escape(request.args.get("token", ""), quote=True)
        return (f"<html><body style='font-family:sans-serif;padding:40px'><h2>Mock PayPal checkout</h2>"
                f"<p>Offline simulation (PAYPAL_MODE=mock). Order: {token}</p>"
                f"<a href='/paypal/return?token={token}'>Pay now</a> &nbsp; "
                f"<a href='/paypal/cancel?token={token}'>Cancel</a></body></html>")

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=False)
