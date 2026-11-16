"""SQLite 저장소. 요청/폴링 스레드마다 연결을 새로 열어 쓴다."""
import contextlib
import sqlite3
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    pop_host          TEXT NOT NULL,
    pop_port          INTEGER NOT NULL,
    tls_name          TEXT NOT NULL,
    mail_user         TEXT NOT NULL,
    mail_password_enc TEXT NOT NULL,
    last_checked_at   TEXT,
    last_error        TEXT
);
CREATE TABLE IF NOT EXISTS pop_uids (
    account_id INTEGER NOT NULL REFERENCES mail_accounts(id) ON DELETE CASCADE,
    uid        TEXT NOT NULL,
    PRIMARY KEY (account_id, uid)
);
CREATE TABLE IF NOT EXISTS mails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid         TEXT NOT NULL,
    sender      TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    mail_date   TEXT NOT NULL,
    attachments TEXT NOT NULL,
    body        TEXT NOT NULL,
    truncated   INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mails_user ON mails(user_id, id);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint   TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    mail_id       INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
    summary       TEXT NOT NULL,
    actions       TEXT NOT NULL,
    reply_needed  INTEGER NOT NULL,
    reply_draft   TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    requested_by  INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_id    INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipients TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    sent_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_settings (
    user_id            INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    skip_bulk          INTEGER NOT NULL DEFAULT 1,
    only_important     INTEGER NOT NULL DEFAULT 0,
    block_senders      TEXT NOT NULL DEFAULT '[]',
    block_keywords     TEXT NOT NULL DEFAULT '[]',
    important_senders  TEXT NOT NULL DEFAULT '[]',
    important_keywords TEXT NOT NULL DEFAULT '[]'
);
-- 사용량 기록은 메일이 지워져도 남아야 하므로 mails 를 참조하지 않는다
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    username      TEXT NOT NULL,
    mail_id       INTEGER,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at);
"""

# 이전 버전 DB 에 없는 열. 새 DB 에도 같은 방식으로 추가된다.
ADDED_COLUMNS = [
    ("users", "is_owner", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "user_agent", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "last_used_at", "TEXT"),
    ("mail_accounts", "smtp_host", "TEXT NOT NULL DEFAULT 'smtp.cafe24.com'"),
    ("mail_accounts", "smtp_port", "INTEGER NOT NULL DEFAULT 587"),
    ("mails", "message_id", "TEXT NOT NULL DEFAULT ''"),
    ("mails", "reply_to", "TEXT NOT NULL DEFAULT ''"),
    ("mails", "is_bulk", "INTEGER NOT NULL DEFAULT 0"),
    ("mails", "notified", "INTEGER NOT NULL DEFAULT 1"),
    ("push_subscriptions", "user_agent", "TEXT NOT NULL DEFAULT ''"),
    ("push_subscriptions", "session_hash", "TEXT"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def connect():
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, column, ddl in ADDED_COLUMNS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # 최초 관리자 표시가 없으면, 가장 먼저 만들어진 관리자를 최초 관리자로 지정한다
        if not conn.execute("SELECT 1 FROM users WHERE is_owner = 1").fetchone():
            conn.execute(
                "UPDATE users SET is_owner = 1 WHERE id ="
                " (SELECT MIN(id) FROM users WHERE role = 'admin' AND is_active = 1)")
        # 사용량 기록이 생기기 전에 분석한 건을 한 번만 옮겨 온다
        if not conn.execute("SELECT 1 FROM usage_log LIMIT 1").fetchone():
            from . import usage
            for r in conn.execute(
                    "SELECT a.*, u.username FROM analyses a JOIN users u ON u.id = a.requested_by").fetchall():
                conn.execute(
                    "INSERT INTO usage_log (user_id, username, mail_id, model, input_tokens, output_tokens,"
                    " cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["requested_by"], r["username"], r["mail_id"], r["model"], r["input_tokens"],
                     r["output_tokens"], usage.cost_usd(r["model"], r["input_tokens"], r["output_tokens"]),
                     r["created_at"]))


def delete_sessions(conn, where: str, params: tuple) -> None:
    """세션을 지우고, 그 세션에서 켠 푸시 알림 등록도 함께 지운다."""
    conn.execute(f"DELETE FROM sessions WHERE {where}", params)
    conn.execute("DELETE FROM push_subscriptions WHERE session_hash IS NOT NULL"
                 " AND session_hash NOT IN (SELECT token_hash FROM sessions)")
