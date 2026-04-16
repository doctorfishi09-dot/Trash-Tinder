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


def _der_to_b64url(der_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(der_bytes).rstrip(b"=").decode("ascii")


def _private_to_b64url_der(private_key) -> str:
    """Serialize a cryptography EC private key to base64url-DER (the format
    pywebpush's `vapid_private_key` parameter actually accepts)."""
    der = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return _der_to_b64url(der)


def _generate_vapid_pair() -> tuple:
    """Make a fresh VAPID keypair. (private_b64url_der, public_b64url_str)."""
    vapid = Vapid01()
    vapid.generate_keys()
    priv_b64 = _private_to_b64url_der(vapid.private_key)
    public_raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")
    return priv_b64, public_b64


def _normalize_stored_private(stored: str) -> str:
    """Old code stored the private key as a PEM string, but pywebpush wants
    base64url-encoded DER. Convert PEM in place; pass through anything that
    already looks like base64url. Keeps the public key intact so existing
    subscriptions keep working."""
    if stored and "-----BEGIN" in stored:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(stored.encode("utf-8"), password=None)
        return _private_to_b64url_der(key)
    return stored


def _load_or_create_keys():
    """Return (private_b64url, public_b64url). Creates on first call.
    Cached in-process."""
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
        elif _HAS_VAPID:
            normalized = _normalize_stored_private(priv)
            if normalized != priv:
                db.set_setting("vapid_private_key", normalized)
                priv = normalized
        _cached_keys["private_pem"] = priv
        _cached_keys["public_b64"] = pub
        return priv, pub


def get_public_key() -> str:
    """Returns the base64url public key for the client, or None if unavailable."""
    if not _HAS_VAPID:
        return None
    _, pub = _load_or_create_keys()
    return pub


def _send_now(subs: list, payload: dict, priv: str) -> None:
    """The actual blocking send loop — runs on a background thread so the
    request handler doesn't have to wait on FCM round-trips."""
    body = json.dumps(payload)
    claims = {"sub": VAPID_SUBJECT}
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


def send_to_subscriptions(subs: list, payload: dict) -> None:
    """Dispatch `payload` to each subscription on a daemon thread so the
    caller (a request handler) returns immediately. Subscriptions that
    come back as 404/410 are pruned from the database asynchronously.
    """
    if not subs or not is_available():
        return
    priv, _ = _load_or_create_keys()
    if not priv:
        return
    threading.Thread(
        target=_send_now, args=(list(subs), payload, priv), daemon=True
    ).start()


# ---------- high-level triggers ----------

def _household_name(household_id: str) -> str:
    hh = db.get_household(household_id)
    return hh["name"] if hh else ""


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
    hh_name = _household_name(household_id)
    payload = {
        "kind": "new_item",
        "title": f"New item in {hh_name}" if hh_name else "New item to vote on",
        "body": f"{creator_name} added {title_part}.",
        "household_id": household_id,
        "item_id": item["id"],
    }
    send_to_subscriptions(subs, payload)


def notify_deadline_warning(item: dict) -> None:
    """Push a 'voting closes soon' warning to users in the item's household
    who still have not cast a real (keep/toss) vote."""
    if not is_available():
        return
    pending_user_ids = db.users_pending_real_vote(item["id"])
    if not pending_user_ids:
        return
    subs = db.list_push_subscriptions_for_users(pending_user_ids)
    if not subs:
        return
    title_part = item.get("title") or "an item"
    hh_name = _household_name(item.get("household_id", ""))
    payload = {
        "kind": "deadline_warning",
        "title": f"Voting closes in 1 hour ({hh_name})" if hh_name else "Voting closes in 1 hour",
        "body": f"You haven't voted on {title_part} yet.",
        "household_id": item.get("household_id"),
        "item_id": item["id"],
    }
    send_to_subscriptions(subs, payload)


def notify_item_decided(item_id: str) -> None:
    """Push the result of a closed item to everyone in the household."""
    if not is_available():
        return
    item = db.item_with_tally(item_id)
    if not item or item.get("status") not in ("kept", "tossed"):
        return
    hh_id = item["household_id"]
    subs = db.list_push_subscriptions_for_household(hh_id)
    if not subs:
        return
    title_part = item.get("title") or "an item"
    tally = item.get("tally") or {}
    keeps = tally.get("keep", 0)
    tosses = tally.get("toss", 0)
    verdict = "kept" if item["status"] == "kept" else "tossed"
    hh_name = _household_name(hh_id)
    payload = {
        "kind": "item_decided",
        "title": f"{title_part} was {verdict}" if title_part != "an item" else f"An item was {verdict}",
        "body": f"{keeps} keep · {tosses} toss" + (f" — {hh_name}" if hh_name else ""),
        "household_id": hh_id,
        "item_id": item_id,
    }
    send_to_subscriptions(subs, payload)


def notify_outcomes(outcomes) -> None:
    """Convenience: take a list of {item_id, outcome, ...} dicts (as returned
    by db.finalize_* helpers) and fire item_decided pushes for each."""
    for o in outcomes or []:
        if not o:
            continue
        item_id = o.get("item_id")
        if item_id:
            try:
                notify_item_decided(item_id)
            except Exception:
                traceback.print_exc()


def notify_member_joined(household_id: str, new_user_id: str) -> None:
    """Push 'X joined the household' to every existing member except the
    person who just joined."""
    if not is_available():
        return
    subs = db.list_push_subscriptions_for_household(
        household_id, exclude_user_id=new_user_id
    )
    if not subs:
        return
    user = db.get_user_by_id(new_user_id)
    name = user["name"] if user else "Someone"
    hh_name = _household_name(household_id)
    payload = {
        "kind": "member_joined",
        "title": f"{name} joined {hh_name}" if hh_name else f"{name} joined",
        "body": "Say hi in the Who tab.",
        "household_id": household_id,
    }
    send_to_subscriptions(subs, payload)


def sweep_deadline_warnings() -> None:
    """Find items hitting their 1-hour mark and warn non-voters."""
    if not is_available():
        return
    items = db.items_needing_deadline_warning(window_seconds=3600)
    for it in items:
        notify_deadline_warning(it)
        # Mark unconditionally so we don't re-check this item every minute.
        db.mark_item_deadline_notified(it["id"])
