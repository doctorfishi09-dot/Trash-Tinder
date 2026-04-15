"""SQLite data layer for the Trash Tinder household app.

Schema: households (with invite_code + per-household lock), users (scoped to a
household), items (scoped), votes, settings. Everything the app cares about is
scoped to a household — a single deployment can host many independent households
sharing one database.

Finalization rules (per item):
  - round closes when every expected member has cast a real vote (keep or toss)
    OR 24h elapsed with at least one real vote.
  - decision uses the item's voting_rule: 'keep_wins' (any keep -> kept; else
    tossed) or 'majority' (more keeps than tosses -> kept; ties go to kept).
  - 'skip' is a personal defer — it does not count toward the threshold and
    the item keeps waiting for that user to make up their mind.
"""

import os
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")
PHOTOS_DIR = os.path.join(BASE_DIR, "data", "photos")

ROUND_TIMEOUT_SECONDS = 24 * 60 * 60  # 24 hours

# Invite-code alphabet — ambiguous characters (0/O, 1/I/L) removed.
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    invite_code TEXT NOT NULL UNIQUE,
    expected_voters INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (household_id, name),
    FOREIGN KEY (household_id) REFERENCES households(id)
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    photo_path TEXT NOT NULL,
    title TEXT,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    round_started_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | kept | tossed
    decided_at INTEGER,
    voting_rule TEXT NOT NULL DEFAULT 'keep_wins',  -- keep_wins | majority
    FOREIGN KEY (household_id) REFERENCES households(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS votes (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    choice TEXT NOT NULL,
    voted_at INTEGER NOT NULL,
    UNIQUE (item_id, user_id, round),
    FOREIGN KEY (item_id) REFERENCES items(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_votes_item_round ON votes(item_id, round);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> int:
    return int(time.time())


def new_id() -> str:
    return uuid.uuid4().hex


def _generate_invite_code(conn) -> str:
    """Make a short, unique, easy-to-type invite code like 'M4G-7XP'."""
    for _ in range(50):
        part1 = "".join(random.choice(INVITE_ALPHABET) for _ in range(3))
        part2 = "".join(random.choice(INVITE_ALPHABET) for _ in range(3))
        code = f"{part1}-{part2}"
        clash = conn.execute(
            "SELECT 1 FROM households WHERE invite_code = ?", (code,)
        ).fetchone()
        if not clash:
            return code
    raise RuntimeError("could not generate unique invite code")


# ---------- init / migrate ----------

def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """In-place migrations for older schemas.

    - Adds items.voting_rule
    - Renames old 'dunno' votes to 'skip'
    - Adds households + scopes users and items to a default household
    """
    # Ensure we're outside a transaction so PRAGMA can take effect.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    # Voting rule column.
    item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "voting_rule" not in item_cols:
        conn.execute(
            "ALTER TABLE items "
            "ADD COLUMN voting_rule TEXT NOT NULL DEFAULT 'keep_wins'"
        )

    # Old 'dunno' votes become 'skip'.
    conn.execute("UPDATE votes SET choice = 'skip' WHERE choice = 'dunno'")

    # Household scoping migration.
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "household_id" not in user_cols:
        _migrate_users_to_households(conn)

    item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "household_id" not in item_cols:
        _migrate_items_to_households(conn)

    # Indexes that depend on household_id are created here, after the column
    # is guaranteed to exist on both users and items.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_household_status "
        "ON items(household_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_household "
        "ON users(household_id)"
    )

    conn.commit()


def _migrate_users_to_households(conn) -> None:
    """Recreate users table with household_id and move legacy rows."""
    legacy_user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    default_household_id = None
    if legacy_user_count > 0:
        default_household_id = new_id()
        default_code = _generate_invite_code(conn)
        expected = 0
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'expected_voters'"
        ).fetchone()
        if row and row["value"]:
            try:
                expected = int(row["value"])
            except (TypeError, ValueError):
                expected = 0
        conn.execute(
            """INSERT INTO households
               (id, name, invite_code, expected_voters, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (default_household_id, "My household", default_code, expected, now()),
        )

    conn.execute("ALTER TABLE users RENAME TO _users_old")
    conn.execute(
        """CREATE TABLE users (
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE (household_id, name),
            FOREIGN KEY (household_id) REFERENCES households(id)
        )"""
    )
    if default_household_id:
        conn.execute(
            "INSERT INTO users (id, household_id, name, created_at) "
            "SELECT id, ?, name, created_at FROM _users_old",
            (default_household_id,),
        )
    conn.execute("DROP TABLE _users_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_household ON users(household_id)")


def _migrate_items_to_households(conn) -> None:
    """Add household_id to items and backfill from existing users."""
    conn.execute(
        "ALTER TABLE items ADD COLUMN household_id TEXT NOT NULL DEFAULT ''"
    )
    # Backfill from the creator's household.
    conn.execute(
        """UPDATE items SET household_id = (
               SELECT household_id FROM users WHERE users.id = items.created_by
           ) WHERE household_id = ''"""
    )
    # Orphan items (creator has been removed) land in the oldest household if
    # there is one. If there's nothing (empty DB) we leave them alone — there
    # shouldn't be any items in that case anyway.
    conn.execute(
        """UPDATE items SET household_id = (
               SELECT id FROM households ORDER BY created_at LIMIT 1
           ) WHERE household_id = ''"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_household_status "
        "ON items(household_id, status)"
    )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- households ----------

def create_household(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    name = name[:80]
    hid = new_id()
    with get_conn() as conn:
        code = _generate_invite_code(conn)
        conn.execute(
            """INSERT INTO households
               (id, name, invite_code, expected_voters, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (hid, name, code, now()),
        )
    return get_household(hid)


def get_household(household_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM households WHERE id = ?", (household_id,)
        ).fetchone()
        return dict(row) if row else None


def get_household_by_code(code: str):
    code = (code or "").strip().upper()
    if not code:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM households WHERE invite_code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


def update_household_name(household_id: str, name: str) -> bool:
    name = (name or "").strip()[:80]
    if not name:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE households SET name = ? WHERE id = ?", (name, household_id)
        )
    return True


def list_households() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM households ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- users ----------

def create_or_get_user(household_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    name = name[:50]
    if not get_household(household_id):
        raise ValueError("household not found")
    existing = get_user_by_name(household_id, name)
    if existing:
        return existing
    uid = new_id()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, household_id, name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (uid, household_id, name, now()),
            )
        except sqlite3.IntegrityError:
            return get_user_by_name(household_id, name)
    return get_user_by_id(uid)


def get_user_by_id(uid: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return dict(row) if row else None


def get_user_by_name(household_id: str, name: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE household_id = ? AND name = ?",
            (household_id, name),
        ).fetchone()
        return dict(row) if row else None


def list_users(household_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE household_id = ? ORDER BY created_at",
            (household_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(user_id: str) -> dict:
    """Remove a user from their household.

    Their votes are deleted. Items they uploaded stay (with an orphan
    created_by). The containing household's lock threshold shrinks to fit the
    remaining member count; if the last member leaves, the household unlocks.

    Returns {"ok": bool, "household_id": str|None, "outcomes": [...]}.
    """
    user = get_user_by_id(user_id)
    if not user:
        return {"ok": False, "household_id": None, "outcomes": []}
    hh_id = user["household_id"]

    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM votes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    if is_locked(hh_id):
        remaining = len(list_users(hh_id))
        if remaining == 0:
            set_expected_voters(hh_id, 0)
        elif get_expected_voters(hh_id) > remaining:
            set_expected_voters(hh_id, remaining)

    outcomes = finalize_all_pending(hh_id)
    return {"ok": True, "household_id": hh_id, "outcomes": outcomes}


# ---------- items ----------

def create_item(
    household_id: str,
    photo_path: str,
    title: str,
    note: str,
    created_by: str,
    voting_rule: str = "keep_wins",
) -> dict:
    if voting_rule not in ("keep_wins", "majority"):
        voting_rule = "keep_wins"
    if not get_household(household_id):
        raise ValueError("household not found")
    iid = new_id()
    ts = now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO items
               (id, household_id, photo_path, title, note, created_by,
                created_at, round, round_started_at, status, voting_rule)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'pending', ?)""",
            (
                iid, household_id, photo_path, title, note, created_by,
                ts, ts, voting_rule,
            ),
        )
    return get_item(iid)


def delete_item(item_id: str) -> bool:
    """Remove an item, its votes, and its photo file from disk."""
    item = get_item(item_id)
    if not item:
        return False
    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM votes WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    try:
        photo_path = os.path.join(PHOTOS_DIR, item["photo_path"])
        if os.path.isfile(photo_path):
            os.remove(photo_path)
    except OSError:
        pass
    return True


def delete_all_done_items(household_id: str) -> int:
    """Remove every kept/tossed item in a household (and its photo). Returns count."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE household_id = ? "
            "AND status IN ('kept', 'tossed')",
            (household_id,),
        ).fetchall()
        ids = [r["id"] for r in rows]
    for iid in ids:
        delete_item(iid)
    return len(ids)


def get_item(iid: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
        return dict(row) if row else None


def list_items(household_id: str, status: str = None) -> list:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM items WHERE household_id = ? AND status = ? "
                "ORDER BY created_at DESC",
                (household_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items WHERE household_id = ? "
                "ORDER BY created_at DESC",
                (household_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_deck_for_user(user_id: str) -> list:
    """Pending items in this user's household they haven't decided yet.

    Items they've skipped stay in the deck but appear at the end.
    """
    user = get_user_by_id(user_id)
    if not user:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.*, v.choice AS my_vote
               FROM items i
               LEFT JOIN votes v
                 ON v.item_id = i.id
                AND v.user_id = ?
                AND v.round = i.round
               WHERE i.status = 'pending'
                 AND i.household_id = ?
                 AND (v.choice IS NULL OR v.choice = 'skip')
               ORDER BY
                 CASE WHEN v.choice = 'skip' THEN 1 ELSE 0 END ASC,
                 i.created_at ASC""",
            (user_id, user["household_id"]),
        ).fetchall()
        return [dict(r) for r in rows]


def item_with_tally(item_id: str):
    item = get_item(item_id)
    if not item:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT choice FROM votes WHERE item_id = ? AND round = ?",
            (item_id, item["round"]),
        ).fetchall()
    tally = {"keep": 0, "toss": 0, "skip": 0}
    for r in rows:
        c = r["choice"]
        tally[c] = tally.get(c, 0) + 1
    item["tally"] = tally
    item["vote_count"] = tally["keep"] + tally["toss"]
    item["skip_count"] = tally["skip"]
    return item


def get_item_detail(item_id: str):
    """Item + per-member vote breakdown, scoped to the item's household."""
    item = item_with_tally(item_id)
    if not item:
        return None
    users = list_users(item["household_id"])
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, choice, voted_at FROM votes "
            "WHERE item_id = ? AND round = ?",
            (item_id, item["round"]),
        ).fetchall()
    by_user = {r["user_id"]: dict(r) for r in rows}
    votes = []
    for u in users:
        v = by_user.pop(u["id"], None)
        votes.append(
            {
                "user_id": u["id"],
                "user_name": u["name"],
                "choice": v["choice"] if v else None,
                "voted_at": v["voted_at"] if v else None,
            }
        )
    for ghost_id, v in by_user.items():
        votes.append(
            {
                "user_id": ghost_id,
                "user_name": "(removed)",
                "choice": v["choice"],
                "voted_at": v["voted_at"],
            }
        )
    item["votes"] = votes
    return item


# ---------- votes ----------

def record_vote(item_id: str, user_id: str, choice: str) -> bool:
    """Record (or overwrite) a user's reaction to an item in the current round.

    Enforces that the voter and item belong to the same household.
    """
    if choice == "dunno":
        choice = "skip"
    if choice not in ("keep", "toss", "skip"):
        return False
    user = get_user_by_id(user_id)
    item = get_item(item_id)
    if not user or not item:
        return False
    if user["household_id"] != item["household_id"]:
        return False
    if item["status"] != "pending":
        return False
    vid = new_id()
    ts = now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO votes (id, item_id, user_id, round, choice, voted_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id, user_id, round) DO UPDATE SET
                   choice = excluded.choice,
                   voted_at = excluded.voted_at""",
            (vid, item_id, user_id, item["round"], choice, ts),
        )
    return True


def get_votes_for_round(item_id: str, round_num: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM votes WHERE item_id = ? AND round = ?",
            (item_id, round_num),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- settings / household lock ----------

def get_setting(key: str, default: str = None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_expected_voters(household_id: str) -> int:
    hh = get_household(household_id)
    if not hh:
        return 0
    return int(hh.get("expected_voters") or 0)


def set_expected_voters(household_id: str, n: int) -> None:
    n = max(0, int(n))
    with get_conn() as conn:
        conn.execute(
            "UPDATE households SET expected_voters = ? WHERE id = ?",
            (n, household_id),
        )


def is_locked(household_id: str) -> bool:
    return get_expected_voters(household_id) > 0


# ---------- decision logic ----------

def finalize_if_ready(item_id: str):
    """Close the item if enough real votes have come in (or the timer expired).

    Closing condition:
      - household is locked AND every expected member has voted keep/toss, OR
      - 24h has elapsed AND at least one real vote exists.

    Decision follows the item's voting_rule.
    """
    item = get_item(item_id)
    if not item or item["status"] != "pending":
        return None

    votes = get_votes_for_round(item_id, item["round"])
    real_votes = [v for v in votes if v["choice"] in ("keep", "toss")]
    real_voter_count = len({v["user_id"] for v in real_votes})
    expected = get_expected_voters(item["household_id"])

    all_voted = expected > 0 and real_voter_count >= expected
    time_up = (now() - item["round_started_at"]) >= ROUND_TIMEOUT_SECONDS

    if not (all_voted or time_up):
        return None

    if real_voter_count == 0:
        _extend_round(item_id)
        return None

    choices = [v["choice"] for v in real_votes]
    rule = item.get("voting_rule") or "keep_wins"

    if rule == "majority":
        keeps = choices.count("keep")
        tosses = choices.count("toss")
        outcome = "kept" if keeps >= tosses else "tossed"
    else:
        outcome = "kept" if "keep" in choices else "tossed"

    _set_status(item_id, outcome)
    return {"item_id": item_id, "outcome": outcome}


def _set_status(item_id: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET status = ?, decided_at = ? WHERE id = ?",
            (status, now(), item_id),
        )


def _extend_round(item_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET round_started_at = ? WHERE id = ?",
            (now(), item_id),
        )


def finalize_all_timed_out() -> list:
    cutoff = now() - ROUND_TIMEOUT_SECONDS
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE status = 'pending' AND round_started_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [r["id"] for r in rows]
    results = []
    for iid in ids:
        outcome = finalize_if_ready(iid)
        if outcome:
            results.append(outcome)
    return results


def finalize_all_pending(household_id: str = None) -> list:
    with get_conn() as conn:
        if household_id:
            rows = conn.execute(
                "SELECT id FROM items WHERE household_id = ? AND status = 'pending'",
                (household_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM items WHERE status = 'pending'"
            ).fetchall()
        ids = [r["id"] for r in rows]
    results = []
    for iid in ids:
        outcome = finalize_if_ready(iid)
        if outcome:
            results.append(outcome)
    return results


def stats(household_id: str) -> dict:
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM items WHERE household_id = ? AND status = 'pending'",
            (household_id,),
        ).fetchone()[0]
        kept = conn.execute(
            "SELECT COUNT(*) FROM items WHERE household_id = ? AND status = 'kept'",
            (household_id,),
        ).fetchone()[0]
        tossed = conn.execute(
            "SELECT COUNT(*) FROM items WHERE household_id = ? AND status = 'tossed'",
            (household_id,),
        ).fetchone()[0]
        users_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE household_id = ?", (household_id,)
        ).fetchone()[0]
    return {
        "pending": pending,
        "kept": kept,
        "tossed": tossed,
        "users": users_count,
    }
