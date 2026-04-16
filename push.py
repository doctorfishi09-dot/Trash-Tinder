"""Web Push notifications for Trash Tinder.

Wraps `pywebpush` so the rest of the app can fire-and-forget. If the
pywebpush dependency is not installed, every send becomes a silent no-op
so the app keeps working — install with `pip install pywebpush` to enable.

VAPID keys are generated on first use and persisted in the `settings` table
so every browser sees a stable application server key. The public key is
served to clients as a base64url string suitable for `applicationServerKey`.

Note for PythonAnywhere free tier: outbound HTTPS is restricted to a
whitelist. Google's FCM endpoints (push for Chrome/Edge/Android) are
generally allowed; Mozilla autopush and Apple endpoints may be blocked,
which means sends to Firefox/Safari clients will silently fail.
"""

import base64
import json
import threading
import traceback

import db

VAPID_SUBJECT = "mailto:noreply@trash-tinder.local"

_keys_lock = threading.Lock()
_cached_keys = {"private_pem": None, "public_b64": None}

try:
    from pywebpush import webpush, WebPushException  # noqa: F401
    _HAS_PYWEBPUSH = True
except Exception:
    _HAS_PYWEBPUSH = False

try:
    from py_vapid import Vapid01
    from cryptography.hazmat.primitives import serialization
    _HAS_VAPID = True
except Exception:
    _HAS_VAPID = False


def is_available() -> bool:
    return _HAS_PYWEBPUSH and _HAS_VAPID


def _generate_vapid_pair() -> tuple:
    """Make a fresh VAPID keypair. (private_pem_str, public_b64url_str)."""
    vapid = Vapid01()
    vapid.generate_keys()
    private_pem = vapid.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")
    return private_pem, public_b64


def _load_or_create_keys():
    """Return (private_pem, public_b64). Creates on first call. Cached in-process."""
    if _cached_keys["private_pem"] and _cached_keys["public_b64"]:
        return _cached_keys["private_pem"], _cached_keys["public_b64"]
    with _keys_lock:
        if _cached_keys["private_pem"] and _cached_keys["public_b64"]:
            return _cached_keys["private_pem"], _cached_keys["public_b64"]
        priv = db.get_setting("vapid_private_key")
        pub = db.get_setting("vapid_public_key")
        if not (priv and pub):
            if not _HAS_VAPID:
                return None, None
            priv, pub = _generate_vapid_pair()
            db.set_setting("vapid_private_key", priv)
            db.set_setting("vapid_public_key", pub)
        _cached_keys["private_pem"] = priv
        _cached_keys["public_b64"] = pub
        return priv, pub


def get_public_key() -> str:
    """Returns the base64url public key for the client, or None if unavailable."""
    if not _HAS_VAPID:
        return None
    _, pub = _load_or_create_keys()
    return pub


def send_to_subscriptions(subs: list, payload: dict) -> int:
    """Push `payload` to each subscription. Returns successful send count.

    Subscriptions that come back as 404/410 (expired/unsubscribed) are
    pruned from the database. Other errors are logged but not raised.
    """
    if not subs or not is_available():
        return 0
    priv, _ = _load_or_create_keys()
    if not priv:
        return 0
    body = json.dumps(payload)
    claims = {"sub": VAPID_SUBJECT}
    sent = 0
    for s in subs:
        sub_info = {
            "endpoint": s["endpoint"],
            "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=body,
                vapid_private_key=priv,
                vapid_claims=dict(claims),
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                try:
                    db.delete_push_subscription_by_endpoint(s["endpoint"])
                except Exception:
                    pass
            else:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
    return sent


# ---------- high-level triggers ----------

def notify_new_item(item: dict, household_id: str, creator_user_id: str) -> None:
    """Fire a 'someone added an item' push to everyone in the household
    except the user who just added it."""
    if not is_available():
        return
    subs = db.list_push_subscriptions_for_household(
        household_id, exclude_user_id=creator_user_id
    )
    if not subs:
        return
    creator = db.get_user_by_id(creator_user_id)
    creator_name = creator["name"] if creator else "Someone"
    title_part = item.get("title") or "a new item"
    payload = {
        "kind": "new_item",
        "title": "New item to vote on",
        "body": f"{creator_name} added {title_part}.",
        "url": "/",
        "item_id": item["id"],
    }
    send_to_subscriptions(subs, payload)


def notify_deadline_warning(item: dict) -> int:
    """Push a 'voting closes soon' warning to users in the item's household
    who still have not cast a real (keep/toss) vote. Returns send count."""
    if not is_available():
        return 0
    pending_user_ids = db.users_pending_real_vote(item["id"])
    if not pending_user_ids:
        return 0
    subs = db.list_push_subscriptions_for_users(pending_user_ids)
    if not subs:
        return 0
    title_part = item.get("title") or "an item"
    payload = {
        "kind": "deadline_warning",
        "title": "Voting closes in 1 hour",
        "body": f"You haven't voted on {title_part} yet.",
        "url": "/",
        "item_id": item["id"],
    }
    return send_to_subscriptions(subs, payload)


def sweep_deadline_warnings() -> int:
    """Find items hitting their 1-hour mark and warn non-voters. Returns
    total push messages sent."""
    if not is_available():
        return 0
    items = db.items_needing_deadline_warning(window_seconds=3600)
    total = 0
    for it in items:
        total += notify_deadline_warning(it)
        # Mark even when 0 sent so we don't re-check this item every minute.
        db.mark_item_deadline_notified(it["id"])
    return total
