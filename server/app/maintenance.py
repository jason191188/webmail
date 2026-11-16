"""하루 한 번 DB 백업, 오래된 메일 정리."""
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import db
from .config import settings

log = logging.getLogger("maintenance")

CHECK_INTERVAL_SEC = 3600
BACKUP_NAME = re.compile(r"^app-\d{8}-\d{6}\.db$")
_backup_lock = threading.Lock()


def list_backups() -> list[dict]:
    if not settings.backup_dir.exists():
        return []
    files = sorted((p for p in settings.backup_dir.iterdir() if BACKUP_NAME.match(p.name)), reverse=True)
    return [{"name": p.name, "size_bytes": p.stat().st_size,
             "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for p in files]


def backup_now() -> dict:
    """서버를 멈추지 않고 DB 전체를 복사한다. 오래된 백업은 backup_keep 개만 남긴다."""
    with _backup_lock:
        settings.backup_dir.mkdir(parents=True, exist_ok=True)
        name = f"app-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
        target = settings.backup_dir / name
        partial = target.with_suffix(".partial")
        src = sqlite3.connect(settings.db_path, timeout=30)
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        partial.replace(target)
        for old in list_backups()[settings.backup_keep:]:
            (settings.backup_dir / old["name"]).unlink(missing_ok=True)
        log.info("백업 완료: %s", name)
        return next(b for b in list_backups() if b["name"] == name)


def backup_if_due() -> None:
    today = f"app-{datetime.now(timezone.utc):%Y%m%d}-"
    if not any(b["name"].startswith(today) for b in list_backups()):
        backup_now()


def purge_old() -> int:
    """보관 기간이 지난 메일(분석 결과, 답장 기록 포함)과 만료된 로그인을 지운다."""
    removed = 0
    with db.connect() as conn:
        if settings.mail_retention_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.mail_retention_days)
                      ).isoformat(timespec="seconds")
            removed = conn.execute("DELETE FROM mails WHERE fetched_at < ?", (cutoff,)).rowcount
        db.delete_sessions(conn, "expires_at <= ?", (db.now(),))
    if removed:
        log.info("보관 기간(%s일)이 지난 메일 %s건을 지웠습니다.", settings.mail_retention_days, removed)
    return removed


class Maintenance(threading.Thread):
    def __init__(self):
        super().__init__(name="maintenance", daemon=True)
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            try:
                backup_if_due()      # 정리하기 전에 먼저 백업한다
                purge_old()
            except Exception:
                log.exception("유지보수 작업 중 오류")
            self._stop_event.wait(CHECK_INTERVAL_SEC)

    def stop(self):
        self._stop_event.set()
