"""SQLite database for mt-backup: routers + backup_logs"""
import sqlite3
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__import__("os").environ.get("MT_DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "panel.sqlite"



# ---- Users CRUD ----

def init_users_table():
    """Create users table if not exists. Idempotent."""
    c = _conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin','viewer')),
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.commit()
    c.close()


def list_users() -> list:
    c = _conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, username, role, enabled, created_at, updated_at FROM users ORDER BY id"
    )]
    c.close()
    return rows


def get_user(user_id: int):
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_user_by_username(username: str):
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    c.close()
    return dict(row) if row else None


def create_user(username: str, password_hash: str, role: str = "viewer", enabled: int = 1) -> int:
    c = _conn()
    now = datetime.utcnow().isoformat()
    cur = c.execute(
        "INSERT INTO users (username, password_hash, role, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (username, password_hash, role, enabled, now, now),
    )
    c.commit()
    new_id = cur.lastrowid
    c.close()
    return new_id


def update_user(user_id: int, role: str = None, enabled: int = None) -> bool:
    c = _conn()
    now = datetime.utcnow().isoformat()
    fields = []
    values = []
    if role is not None:
        fields.append("role = ?"); values.append(role)
    if enabled is not None:
        fields.append("enabled = ?"); values.append(enabled)
    if not fields:
        c.close()
        return False
    fields.append("updated_at = ?"); values.append(now)
    values.append(user_id)
    cur = c.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    c.commit()
    changed = cur.rowcount > 0
    c.close()
    return changed


def update_user_password(user_id: int, password_hash: str) -> bool:
    c = _conn()
    now = datetime.utcnow().isoformat()
    cur = c.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (password_hash, now, user_id),
    )
    c.commit()
    changed = cur.rowcount > 0
    c.close()
    return changed


def delete_user(user_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    c.commit()
    changed = cur.rowcount > 0
    c.close()
    return changed


def verify_user_password(user, plain_password: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(plain_password[:72].encode("utf-8"), user["password_hash"].encode("utf-8"))
    except Exception:
        return False


# Initialize users table on import (called at end of module)

def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS routers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            ip TEXT NOT NULL,
            port INTEGER DEFAULT 2282,
            username TEXT NOT NULL,
            password_encrypted TEXT NOT NULL,
            device_type TEXT NOT NULL DEFAULT 'router' CHECK(device_type IN ('router','switch')),
            location TEXT DEFAULT '',
            identity TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            last_backup_at TEXT,
            last_backup_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS backup_logs (
            id INTEGER PRIMARY KEY,
            router_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('success','failed')),
            size INTEGER DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (router_id) REFERENCES routers(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_logs_router ON backup_logs(router_id);
        CREATE INDEX IF NOT EXISTS idx_logs_created ON backup_logs(created_at);
        """
    )
    c.commit()
    # Idempotent column additions for existing DBs
    for col in ("identity",):
        try:
            c.execute(f"ALTER TABLE routers ADD COLUMN {col} TEXT DEFAULT ''")
            c.commit()
        except Exception:
            pass
    c.close()


# ---- Router CRUD ----

def list_routers() -> list[dict]:
    c = _conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM routers ORDER BY device_type, name"
    )]
    c.close()
    return rows


def get_router(router_id: int) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM routers WHERE id = ?", (router_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def create_router(name, ip, port, username, password_encrypted, device_type, location, enabled) -> int:
    c = _conn()
    now = datetime.utcnow().isoformat()
    cur = c.execute(
        """INSERT INTO routers
           (name, ip, port, username, password_encrypted, device_type, location, enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, ip, port, username, password_encrypted, device_type, location, enabled, now, now),
    )
    c.commit()
    new_id = cur.lastrowid
    c.close()
    return new_id


def update_router(router_id, name, ip, port, username, password_encrypted, device_type, location, enabled) -> bool:
    c = _conn()
    now = datetime.utcnow().isoformat()
    cur = c.execute(
        """UPDATE routers SET
           name=?, ip=?, port=?, username=?, password_encrypted=?,
           device_type=?, location=?, enabled=?, updated_at=?
           WHERE id=?""",
        (name, ip, port, username, password_encrypted, device_type, location, enabled, now, router_id),
    )
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def delete_router(router_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM routers WHERE id = ?", (router_id,))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def set_last_backup(router_id: int, status: str):
    c = _conn()
    c.execute(
        "UPDATE routers SET last_backup_at = ?, last_backup_status = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), status, router_id),
    )
    c.commit()
    c.close()


def set_router_identity(router_id: int, identity: str) -> bool:
    """Save auto-detected RouterOS identity to DB. Returns True if changed."""
    if not identity:
        return False
    c = _conn()
    cur = c.execute(
        "UPDATE routers SET identity = ? WHERE id = ? AND (identity IS NULL OR identity != ?)",
        (identity, router_id, identity),
    )
    c.commit()
    changed = cur.rowcount > 0
    c.close()
    return changed


# ---- Backup log CRUD ----

def log_backup(router_id: int, filename: str, status: str, size: int, error: str | None = None) -> int:
    c = _conn()
    cur = c.execute(
        """INSERT INTO backup_logs (router_id, filename, status, size, error, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (router_id, filename, status, size, error, datetime.utcnow().isoformat()),
    )
    c.commit()
    new_id = cur.lastrowid
    c.close()
    return new_id


def list_logs(limit: int = 50, router_id: int | None = None) -> list[dict]:
    c = _conn()
    if router_id is not None:
        rows = c.execute(
            "SELECT l.*, r.name AS router_name, r.ip AS router_ip FROM backup_logs l "
            "JOIN routers r ON l.router_id = r.id "
            "WHERE l.router_id = ? ORDER BY l.created_at DESC LIMIT ?",
            (router_id, limit),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT l.*, r.name AS router_name, r.ip AS router_ip FROM backup_logs l "
            "JOIN routers r ON l.router_id = r.id "
            "ORDER BY l.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    c = _conn()
    stats = {
        "total_routers": c.execute("SELECT COUNT(*) FROM routers").fetchone()[0],
        "active_routers": c.execute("SELECT COUNT(*) FROM routers WHERE enabled = 1").fetchone()[0],
        "total_switches": c.execute("SELECT COUNT(*) FROM routers WHERE device_type = 'switch'").fetchone()[0],
        "today_success": c.execute(
            "SELECT COUNT(*) FROM backup_logs WHERE status = 'success' "
            "AND date(created_at) = date('now')"
        ).fetchone()[0],
        "today_failed": c.execute(
            "SELECT COUNT(*) FROM backup_logs WHERE status = 'failed' "
            "AND date(created_at) = date('now')"
        ).fetchone()[0],
        "total_backups": c.execute("SELECT COUNT(*) FROM backup_logs").fetchone()[0],
        "total_size_bytes": c.execute(
            "SELECT COALESCE(SUM(size), 0) FROM backup_logs WHERE status = 'success'"
        ).fetchone()[0],
    }
    c.close()
    return stats

# Initialize users table after all defs
init_users_table()
