"""Trash Tinder — WSGI application + local dev server.

The app is a plain WSGI callable (`application`) so it runs unchanged on
PythonAnywhere, gunicorn, uWSGI, Flask's dev server, etc. For local use we
ship a threaded wsgiref-based dev server so `python server.py` still works.

Hosts many households on one SQLite file. Every request carries household
context (either `household_id` on the query/body or implicitly via user/item
IDs). There's no always-on background thread — 24h round timeouts are
finalized lazily during normal requests.

Run locally:     python server.py
Run on PA:       point the WSGI config file at server:application
"""

import cgi
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

import db
import push

STATIC_DIR = os.path.join(db.BASE_DIR, "static")
PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")
PHOTO_MAX_BYTES = 15 * 1024 * 1024  # 15 MB

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SAFE_PHOTO_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

USER_ID_PATH_RE = re.compile(r"^/api/users/([A-Za-z0-9]+)$")
ITEM_ID_PATH_RE = re.compile(r"^/api/items/([A-Za-z0-9]+)$")
ITEM_FINALIZE_PATH_RE = re.compile(r"^/api/items/([A-Za-z0-9]+)/finalize-now$")
HH_ID_PATH_RE = re.compile(r"^/api/households/([A-Za-z0-9]+)$")
HH_CODE_PATH_RE = re.compile(r"^/api/households/by-code/([A-Za-z0-9\-]+)$")

ROOT_STATIC_FILES = {
    "app.js",
    "style.css",
    "manifest.webmanifest",
    "sw.js",
    "icon.svg",
    "icon-192.png",
    "icon-512.png",
}


# ------------------------------------------------------------------
# Lazy timeout sweep
# ------------------------------------------------------------------
# PythonAnywhere's free tier recycles WSGI workers and disallows always-on
# background threads, so we finalize timed-out voting rounds during normal
# request processing. Throttled in-memory so the cost is negligible.

_sweep_lock = threading.Lock()
_last_sweep_at = [0.0]
SWEEP_INTERVAL_SECONDS = 60


def maybe_sweep_timeouts() -> None:
    now_ts = time.time()
    with _sweep_lock:
        if now_ts - _last_sweep_at[0] < SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep_at[0] = now_ts
    try:
        db.finalize_all_timed_out()
    except Exception:
        traceback.print_exc()
    try:
        push.sweep_deadline_warnings()
    except Exception:
        traceback.print_exc()


# ------------------------------------------------------------------
# Response helpers
# ------------------------------------------------------------------

def status_line(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Status"


def json_resp(status: int, obj) -> tuple:
    body = json.dumps(obj).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    return status, headers, body


def text_resp(status: int, text: str, ctype: str = "text/plain; charset=utf-8") -> tuple:
    body = text.encode("utf-8")
    headers = [
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
    ]
    return status, headers, body


def file_resp(filepath: str, cache: bool = False) -> tuple:
    if not os.path.isfile(filepath):
        return text_resp(404, "Not Found")
    ctype = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError:
        return text_resp(404, "Not Found")
    headers = [
        ("Content-Type", ctype),
        ("Content-Length", str(len(data))),
        ("Cache-Control", "public, max-age=3600" if cache else "no-cache"),
    ]
    return 200, headers, data


def safe_photo_path(name: str):
    if not name or not SAFE_PHOTO_NAME_RE.match(name):
        return None
    full = os.path.abspath(os.path.join(db.PHOTOS_DIR, name))
    if not full.startswith(os.path.abspath(db.PHOTOS_DIR) + os.sep):
        return None
    return full


def config_snapshot(household_id: str) -> dict:
    hh = db.get_household(household_id)
    if not hh:
        return {}
    expected = int(hh.get("expected_voters") or 0)
    return {
        "household_id": household_id,
        "locked": expected > 0,
        "expected_voters": expected,
        "user_count": len(db.list_users(household_id)),
    }


def public_household(hh: dict) -> dict:
    return {
        "id": hh["id"],
        "name": hh["name"],
        "invite_code": hh["invite_code"],
        "expected_voters": int(hh.get("expected_voters") or 0),
        "created_at": hh["created_at"],
    }


def read_json(environ) -> "dict | None":
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if length <= 0:
        return {}
    if length > 1024 * 1024:
        return None
    raw = environ["wsgi.input"].read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------
# Multipart photo upload
# ------------------------------------------------------------------

def create_item_from_multipart(environ) -> tuple:
    ctype = environ.get("CONTENT_TYPE", "")
    if "multipart/form-data" not in ctype:
        return json_resp(400, {"error": "expected multipart"})
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if length <= 0:
        return json_resp(400, {"error": "empty body"})
    if length > PHOTO_MAX_BYTES:
        return json_resp(413, {"error": "file too large"})

    try:
        form = cgi.FieldStorage(
            fp=environ["wsgi.input"],
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": ctype,
                "CONTENT_LENGTH": str(length),
            },
            keep_blank_values=True,
        )
    except Exception:
        return json_resp(400, {"error": "bad multipart"})

    user_id = form.getvalue("user_id") or ""
    user = db.get_user_by_id(user_id)
    if not user:
        return json_resp(400, {"error": "unknown user"})
    hh_id = user["household_id"]
    title = (form.getvalue("title") or "").strip()[:100]
    note = (form.getvalue("note") or "").strip()[:500]
    voting_rule = form.getvalue("voting_rule") or "keep_wins"
    if voting_rule not in ("keep_wins", "majority"):
        voting_rule = "keep_wins"
    # time_limit_seconds: int seconds, "0"/"none"/empty/missing -> no limit,
    # unparseable -> 24h default.
    tl_raw = form.getvalue("time_limit_seconds")
    if tl_raw is None or tl_raw == "":
        time_limit_seconds = 86400
    elif str(tl_raw).lower() in ("0", "none", "null"):
        time_limit_seconds = None
    else:
        try:
            time_limit_seconds = int(tl_raw)
            if time_limit_seconds <= 0:
                time_limit_seconds = None
        except (TypeError, ValueError):
            time_limit_seconds = 86400

    if "photo" not in form:
        return json_resp(400, {"error": "missing photo"})
    field = form["photo"]
    if not getattr(field, "file", None) or not field.filename:
        return json_resp(400, {"error": "missing photo"})

    ext = os.path.splitext(field.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".jpg"
    photo_bytes = field.file.read()
    if not photo_bytes:
        return json_resp(400, {"error": "empty photo"})
    if len(photo_bytes) > PHOTO_MAX_BYTES:
        return json_resp(413, {"error": "file too large"})

    new_name = f"{db.new_id()}{ext}"
    out_path = os.path.join(db.PHOTOS_DIR, new_name)
    with open(out_path, "wb") as f:
        f.write(photo_bytes)

    item = db.create_item(
        hh_id, new_name, title, note, user_id,
        voting_rule=voting_rule,
        time_limit_seconds=time_limit_seconds,
    )
    item = db.item_with_tally(item["id"])
    try:
        push.notify_new_item(item, hh_id, user_id)
    except Exception:
        traceback.print_exc()
    return json_resp(200, {"item": item})


# ------------------------------------------------------------------
# Routing
# ------------------------------------------------------------------

def handle(method: str, path: str, qs: dict, environ) -> tuple:
    # Static files first — no sweep needed, no DB touch.
    if method == "GET":
        if path in ("/", "/index.html"):
            return file_resp(os.path.join(STATIC_DIR, "index.html"))
        if path.lstrip("/") in ROOT_STATIC_FILES:
            return file_resp(
                os.path.join(STATIC_DIR, path.lstrip("/")), cache=False
            )
        if path.startswith("/photos/"):
            name = path[len("/photos/"):]
            fp = safe_photo_path(name)
            if not fp:
                return text_resp(404, "Not Found")
            return file_resp(fp, cache=True)

    # Any API request — chance to finalize stale voting rounds.
    if path.startswith("/api/"):
        maybe_sweep_timeouts()

    # ---- GET api ----
    if method == "GET":
        m = HH_CODE_PATH_RE.match(path)
        if m:
            hh = db.get_household_by_code(m.group(1))
            if not hh:
                return json_resp(404, {"error": "household not found"})
            return json_resp(200, {"household": public_household(hh)})

        m = HH_ID_PATH_RE.match(path)
        if m:
            hh = db.get_household(m.group(1))
            if not hh:
                return json_resp(404, {"error": "household not found"})
            return json_resp(200, {"household": public_household(hh)})

        if path == "/api/me":
            uid = (qs.get("user_id") or [""])[0]
            user = db.get_user_by_id(uid) if uid else None
            return json_resp(200, {"user": user})

        if path == "/api/users":
            hh_id = (qs.get("household_id") or [""])[0]
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            return json_resp(200, {"users": db.list_users(hh_id)})

        if path == "/api/deck":
            uid = (qs.get("user_id") or [""])[0]
            user = db.get_user_by_id(uid) if uid else None
            if not user:
                return json_resp(400, {"error": "unknown user"})
            deck_rows = db.get_deck_for_user(uid)
            items = []
            for r in deck_rows:
                tallied = db.item_with_tally(r["id"])
                tallied["my_vote"] = r.get("my_vote")
                items.append(tallied)
            return json_resp(200, {"items": items})

        if path == "/api/items":
            hh_id = (qs.get("household_id") or [""])[0]
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            status_f = (qs.get("status") or [None])[0]
            items = db.list_items(hh_id, status=status_f)
            return json_resp(
                200, {"items": [db.item_with_tally(i["id"]) for i in items]}
            )

        m = ITEM_ID_PATH_RE.match(path)
        if m:
            detail = db.get_item_detail(m.group(1))
            if not detail:
                return json_resp(404, {"error": "item not found"})
            return json_resp(200, {"item": detail})

        if path == "/api/stats":
            hh_id = (qs.get("household_id") or [""])[0]
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            return json_resp(200, db.stats(hh_id))

        if path == "/api/config":
            hh_id = (qs.get("household_id") or [""])[0]
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            return json_resp(200, config_snapshot(hh_id))

        if path == "/api/push/vapid-public-key":
            key = push.get_public_key()
            return json_resp(
                200,
                {"available": push.is_available(), "public_key": key},
            )

        return text_resp(404, "Not Found")

    # ---- POST api ----
    if method == "POST":
        if path == "/api/households":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            name = (data.get("name") or "").strip()
            if not name or len(name) > 80:
                return json_resp(400, {"error": "invalid name"})
            hh = db.create_household(name)
            return json_resp(200, {"household": public_household(hh)})

        if path == "/api/users":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            hh_id = (data.get("household_id") or "").strip()
            name = (data.get("name") or "").strip()
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            if not name or len(name) > 50:
                return json_resp(400, {"error": "invalid name"})
            user = db.create_or_get_user(hh_id, name)
            return json_resp(200, {"user": user})

        if path == "/api/items":
            return create_item_from_multipart(environ)

        m = ITEM_FINALIZE_PATH_RE.match(path)
        if m:
            result = db.finalize_early(m.group(1))
            if not result:
                return json_resp(
                    400,
                    {"error": "need at least one keep or toss vote to finish"},
                )
            return json_resp(200, {"ok": True, "outcome": result})

        if path == "/api/items/clear-done":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            hh_id = (data.get("household_id") or "").strip()
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            deleted = db.delete_all_done_items(hh_id)
            return json_resp(200, {"ok": True, "deleted": deleted})

        if path == "/api/config":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            hh_id = (data.get("household_id") or "").strip()
            if not hh_id or not db.get_household(hh_id):
                return json_resp(400, {"error": "unknown household"})
            if "lock" not in data:
                return json_resp(400, {"error": "missing 'lock'"})
            if data["lock"]:
                count = len(db.list_users(hh_id))
                if count == 0:
                    return json_resp(400, {"error": "no users to lock"})
                db.set_expected_voters(hh_id, count)
                db.finalize_all_pending(hh_id)
            else:
                db.set_expected_voters(hh_id, 0)
            return json_resp(200, {"config": config_snapshot(hh_id)})

        if path == "/api/vote":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            item_id = data.get("item_id")
            user_id = data.get("user_id")
            choice = data.get("choice")
            if not (item_id and user_id and choice):
                return json_resp(400, {"error": "missing fields"})
            ok = db.record_vote(item_id, user_id, choice)
            if not ok:
                return json_resp(400, {"error": "vote rejected"})
            outcome = db.finalize_if_ready(item_id)
            return json_resp(200, {"ok": True, "outcome": outcome})

        if path == "/api/push/subscribe":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            user_id = (data.get("user_id") or "").strip()
            sub = data.get("subscription") or {}
            endpoint = (sub.get("endpoint") or "").strip()
            keys = sub.get("keys") or {}
            p256dh = (keys.get("p256dh") or "").strip()
            auth = (keys.get("auth") or "").strip()
            if not (user_id and endpoint and p256dh and auth):
                return json_resp(400, {"error": "missing fields"})
            ok = db.add_push_subscription(user_id, endpoint, p256dh, auth)
            if not ok:
                return json_resp(400, {"error": "could not subscribe"})
            return json_resp(200, {"ok": True})

        if path == "/api/push/unsubscribe":
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            endpoint = (data.get("endpoint") or "").strip()
            if not endpoint:
                return json_resp(400, {"error": "missing endpoint"})
            db.delete_push_subscription_by_endpoint(endpoint)
            return json_resp(200, {"ok": True})

        return text_resp(404, "Not Found")

    # ---- PATCH api ----
    if method == "PATCH":
        m = HH_ID_PATH_RE.match(path)
        if m:
            data = read_json(environ)
            if data is None:
                return json_resp(400, {"error": "bad json"})
            name = (data.get("name") or "").strip()
            if not name:
                return json_resp(400, {"error": "invalid name"})
            if not db.update_household_name(m.group(1), name):
                return json_resp(404, {"error": "not found"})
            hh = db.get_household(m.group(1))
            return json_resp(200, {"household": public_household(hh)})
        return text_resp(404, "Not Found")

    # ---- DELETE api ----
    if method == "DELETE":
        m = USER_ID_PATH_RE.match(path)
        if m:
            uid = m.group(1)
            result = db.delete_user(uid)
            if not result["ok"]:
                return json_resp(404, {"error": "user not found"})
            hh_id = result["household_id"]
            return json_resp(
                200,
                {
                    "ok": True,
                    "config": config_snapshot(hh_id) if hh_id else {},
                    "outcomes": result.get("outcomes", []),
                },
            )

        m = ITEM_ID_PATH_RE.match(path)
        if m:
            if not db.delete_item(m.group(1)):
                return json_resp(404, {"error": "item not found"})
            return json_resp(200, {"ok": True})

        return text_resp(404, "Not Found")

    return text_resp(405, "Method Not Allowed")


# ------------------------------------------------------------------
# WSGI application entry point
# ------------------------------------------------------------------

_INITED = False
_init_lock = threading.Lock()


def _ensure_db():
    global _INITED
    if _INITED:
        return
    with _init_lock:
        if _INITED:
            return
        db.init_db()
        _INITED = True


def application(environ, start_response):
    _ensure_db()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        status, headers, body = handle(method, path, qs, environ)
    except (BrokenPipeError, ConnectionResetError):
        status, headers, body = 200, [("Content-Type", "text/plain")], b""
    except Exception:
        traceback.print_exc()
        msg = b"Internal error"
        status = 500
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(msg))),
        ]
        body = msg
    start_response(status_line(status), headers)
    return [body]


# ------------------------------------------------------------------
# Local dev server (threaded wsgiref)
# ------------------------------------------------------------------

class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def _silent_log_request(*_args, **_kwargs):
    pass


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    _ensure_db()
    ip = get_local_ip()
    print()
    print("  Trash Tinder is running")
    print(f"    this device:  http://127.0.0.1:{PORT}")
    print(f"    on the WLAN:  http://{ip}:{PORT}")
    print()
    print("  Ctrl+C to stop.")
    print()
    srv = make_server(HOST, PORT, application, server_class=ThreadingWSGIServer)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        srv.shutdown()


if __name__ == "__main__":
    main()
