import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("data/cco.sqlite")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    return c


@contextmanager
def conn():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _ensure_column(c, table, name, definition):
    cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS config(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS superusers(
            user_id TEXT PRIMARY KEY,
            added_by TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS panel_permissions(
            user_id TEXT NOT NULL,
            module TEXT NOT NULL,
            allowed INTEGER NOT NULL DEFAULT 1,
            updated_by TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, module)
        );

        CREATE TABLE IF NOT EXISTS panel_login_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            username TEXT,
            ip TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS members(
            discord_id TEXT PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            internal_id TEXT,
            callsign TEXT UNIQUE,
            rank TEXT,
            tester INTEGER DEFAULT 0,
            head_tester INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            suspended_reason TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS callsign_ranges(
            rank TEXT PRIMARY KEY,
            prefix TEXT NOT NULL,
            start_num INTEGER NOT NULL,
            end_num INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS callsign_reservations(
            callsign TEXT PRIMARY KEY,
            rank TEXT,
            reserved_for TEXT,
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE NOT NULL,
            panel_message_id TEXT,
            creator_id TEXT NOT NULL,
            type TEXT NOT NULL,
            claimed_by TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            closed_by TEXT
        );

        CREATE TABLE IF NOT EXISTS timesheets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            clock_in TEXT NOT NULL,
            clock_out TEXT,
            duration_minutes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            cancelled_by TEXT,
            log_channel_id TEXT,
            log_message_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS timesheet_adjustments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            reason TEXT,
            changed_by TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS security_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS security_whitelist(
            user_id TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'all',
            PRIMARY KEY(user_id, scope)
        );

        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS id_mappings(
            kind TEXT NOT NULL,
            old_id TEXT NOT NULL,
            new_id TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(kind, old_id)
        );

        CREATE TABLE IF NOT EXISTS audit_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            actor_id TEXT,
            target_id TEXT,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lockdown_state(
            channel_id TEXT PRIMARY KEY,
            overwrite_json TEXT NOT NULL,
            saved_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS leadership_callsigns(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            callsign TEXT UNIQUE NOT NULL COLLATE NOCASE,
            rank TEXT,
            assigned_to TEXT UNIQUE,
            note TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS operational_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            author_id TEXT NOT NULL,
            subject_name TEXT,
            subject_cnp TEXT,
            participants_json TEXT DEFAULT '[]',
            data_json TEXT DEFAULT '{}',
            evidence_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id TEXT,
            review_reason TEXT,
            reviewed_at TEXT,
            channel_id TEXT,
            message_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS action_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id TEXT,
            review_reason TEXT,
            valid_from TEXT,
            valid_until TEXT,
            used_record_id INTEGER,
            channel_id TEXT,
            message_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS resignations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            notes TEXT,
            accepts_pk INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id TEXT,
            review_reason TEXT,
            channel_id TEXT,
            message_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS inactivity_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id TEXT,
            review_reason TEXT,
            starts_at TEXT,
            ends_at TEXT,
            channel_id TEXT,
            message_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sanctions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,
            stage INTEGER NOT NULL,
            total_stages INTEGER NOT NULL,
            days INTEGER NOT NULL DEFAULT 7,
            reason TEXT NOT NULL,
            evidence_url TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            starts_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            channel_id TEXT,
            message_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS protected_users(
            user_id TEXT PRIMARY KEY,
            note TEXT,
            added_by TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS member_protection_snapshots(
            user_id TEXT PRIMARY KEY,
            nickname TEXT,
            roles_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS command_permissions(
            command_name TEXT PRIMARY KEY,
            min_level TEXT NOT NULL DEFAULT 'member',
            cooldown_seconds REAL NOT NULL DEFAULT 2,
            updated_by TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS member_rank_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            old_rank TEXT,
            new_rank TEXT,
            changed_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Migrații pentru baze vechi.
        _ensure_column(c, "members", "suspended_reason", "TEXT")
        _ensure_column(c, "tickets", "panel_message_id", "TEXT")
        _ensure_column(c, "timesheets", "log_channel_id", "TEXT")
        _ensure_column(c, "timesheets", "log_message_id", "TEXT")
        _ensure_column(c, "members", "inactivity_until", "TEXT")
        _ensure_column(c, "members", "out_reason", "TEXT")
        _ensure_column(c, "leadership_callsigns", "rank", "TEXT")
        _ensure_column(c, "sanctions", "requested_type", "TEXT")

    defaults = {
        "timezone": "Europe/Bucharest",
        "security_enabled": "1",
        "antiraid_enabled": "1",
        "antiraid_joins": "10",
        "antiraid_window_sec": "15",
        "antispam_enabled": "1",
        "antispam_messages": "6",
        "antispam_window_sec": "5",
        "antispam_timeout_min": "5",
        "antimention_enabled": "1",
        "antimention_max": "5",
        "antimention_timeout_min": "10",
        "antilink_enabled": "0",
        "antilink_allowed_channels": "",
        "antinuke_enabled": "1",
        "channel_delete_limit": "3",
        "channel_delete_window_sec": "15",
        "role_delete_limit": "2",
        "role_delete_window_sec": "15",
        "mass_kick_limit": "5",
        "mass_kick_window_sec": "30",
        "mass_ban_limit": "3",
        "mass_ban_window_sec": "20",
        "webhook_create_limit": "3",
        "webhook_window_sec": "60",
        "dangerous_action": "strip_roles",
        "restore_channels": "1",
        "restore_roles": "1",
        "snapshot_interval_min": "10",
        "min_account_age_days": "0",
        "nickname_protection": "1",
        "role_escalation_protection": "1",
        "role_lock_enabled": "1",
        "role_permission_guard": "1",
        "protected_rank_roles": "1",
        "callsign_duplicate_guard": "1",
        "command_permission_guard": "1",
        "command_cooldown_enabled": "1",
        "protected_users_enabled": "1",
        "nickname_restore_log": "1",
        "member_role_restore": "1",
        "panel_session_minutes": "30",
    }
    with conn() as c:
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO security_settings(key,value) VALUES(?,?)", (k, v))


def fetchone(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchone()


def fetchall(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchall()


def execute(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def executemany(sql, params):
    with conn() as c:
        c.executemany(sql, params)


def get_config(key, fallback=None):
    row = fetchone("SELECT value FROM config WHERE key=?", (key,))
    return row["value"] if row else fallback


def set_config(key, value):
    execute("""
        INSERT INTO config(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))


def get_security(key, fallback=None):
    row = fetchone("SELECT value FROM security_settings WHERE key=?", (key,))
    return row["value"] if row else fallback


def set_security(key, value):
    execute("""
        INSERT INTO security_settings(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))


def is_superuser(user_id):
    return fetchone("SELECT 1 FROM superusers WHERE user_id=?", (str(user_id),)) is not None


def panel_permission(user_id, module, owner_id=None):
    if owner_id and str(user_id) == str(owner_id):
        return True
    if not is_superuser(user_id):
        return False
    row = fetchone(
        "SELECT allowed FROM panel_permissions WHERE user_id=? AND module=?",
        (str(user_id), module),
    )
    # Superuserii au acces implicit; poți restricționa explicit un modul.
    return True if row is None else bool(row["allowed"])


def set_panel_permission(user_id, module, allowed, updated_by=None):
    execute("""
        INSERT INTO panel_permissions(user_id,module,allowed,updated_by,updated_at)
        VALUES(?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(user_id,module) DO UPDATE SET
            allowed=excluded.allowed,
            updated_by=excluded.updated_by,
            updated_at=CURRENT_TIMESTAMP
    """, (str(user_id), module, 1 if allowed else 0, str(updated_by) if updated_by else None))


def audit(event_type, actor_id=None, target_id=None, details=""):
    execute(
        "INSERT INTO audit_events(type,actor_id,target_id,details) VALUES(?,?,?,?)",
        (event_type, str(actor_id) if actor_id else None, str(target_id) if target_id else None, details),
    )


def is_protected_user(user_id):
    if str(user_id) == str(__import__("os").getenv("OWNER_ID", "")):
        return True
    if is_superuser(user_id):
        return True
    return fetchone("SELECT 1 FROM protected_users WHERE user_id=?", (str(user_id),)) is not None


def set_protected_user(user_id, enabled=True, note="", added_by=None):
    if enabled:
        execute("""
            INSERT INTO protected_users(user_id,note,added_by) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET note=excluded.note,added_by=excluded.added_by
        """, (str(user_id), note, str(added_by) if added_by else None))
    else:
        execute("DELETE FROM protected_users WHERE user_id=?", (str(user_id),))


def save_member_protection_snapshot(user_id, nickname, role_ids):
    import json
    execute("""
        INSERT INTO member_protection_snapshots(user_id,nickname,roles_json,updated_at)
        VALUES(?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            nickname=excluded.nickname,roles_json=excluded.roles_json,updated_at=CURRENT_TIMESTAMP
    """, (str(user_id), nickname, json.dumps([str(x) for x in role_ids])))


def get_member_protection_snapshot(user_id):
    return fetchone("SELECT * FROM member_protection_snapshots WHERE user_id=?", (str(user_id),))
