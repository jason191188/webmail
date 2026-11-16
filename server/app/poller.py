"""메일 계정별 새 메일 수집. 수집만 하고 Claude 는 호출하지 않는다."""
import json
import logging
import threading

from . import db, mailfetch, push, security
from .config import settings

log = logging.getLogger("poller")

MAX_PER_SYNC = 20
_sync_lock = threading.Lock()      # 수동 동기화와 주기 폴링이 겹치지 않게 한다


def sync_account(account_id: int) -> int:
    """계정 하나를 확인해 새 메일을 저장하고, 저장한 건수를 돌려준다."""
    with _sync_lock:
        with db.connect() as conn:
            acc = conn.execute("SELECT * FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
            if acc is None:
                return 0
            known = {r["uid"] for r in conn.execute(
                "SELECT uid FROM pop_uids WHERE account_id = ?", (account_id,))}
        try:
            saved = _fetch_new(acc, known)
        except Exception as e:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE mail_accounts SET last_checked_at = ?, last_error = ? WHERE id = ?",
                    (db.now(), f"{type(e).__name__}: {e}"[:300], account_id))
            raise
        with db.connect() as conn:
            conn.execute(
                "UPDATE mail_accounts SET last_checked_at = ?, last_error = NULL WHERE id = ?",
                (db.now(), account_id))
        return saved


def _fetch_new(acc, known: set[str]) -> int:
    pop = mailfetch.connect(acc["pop_host"], acc["pop_port"], acc["tls_name"],
                            acc["mail_user"], security.decrypt(acc["mail_password_enc"]))
    saved = []
    try:
        uids = mailfetch.list_uids(pop)
        new = sorted((num, uid) for uid, num in uids.items() if uid not in known)
        for num, uid in new[:MAX_PER_SYNC]:
            mail = mailfetch.parse_mail(mailfetch.fetch_raw(pop, num))
            with db.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO mails (user_id, uid, sender, recipient, subject, mail_date,"
                    " attachments, body, truncated, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (acc["user_id"], uid, mail["sender"], mail["recipient"], mail["subject"],
                     mail["mail_date"], json.dumps(mail["attachments"], ensure_ascii=False),
                     mail["body"], int(mail["truncated"]), db.now()))
                conn.execute("INSERT OR IGNORE INTO pop_uids (account_id, uid) VALUES (?, ?)",
                             (acc["id"], uid))
            saved.append({"id": cur.lastrowid, "subject": mail["subject"], "sender": mail["sender"]})
        # 서버에서 지워진 메일의 uid 기록은 정리한다
        gone = known - set(uids)
        if gone:
            with db.connect() as conn:
                conn.executemany("DELETE FROM pop_uids WHERE account_id = ? AND uid = ?",
                                 [(acc["id"], u) for u in gone])
    finally:
        try:
            pop.quit()
        except Exception:
            pass
        push.notify_new_mails(acc["user_id"], saved)     # 중간에 실패해도 저장된 메일은 알린다
    return len(saved)


def sync_all() -> None:
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT a.id FROM mail_accounts a JOIN users u ON u.id = a.user_id WHERE u.is_active = 1")]
    for account_id in ids:
        try:
            saved = sync_account(account_id)
            if saved:
                log.info("account %s: 새 메일 %s건 저장", account_id, saved)
        except Exception as e:
            log.warning("account %s: 확인 실패 (%s: %s)", account_id, type(e).__name__, e)


class Poller(threading.Thread):
    def __init__(self):
        super().__init__(name="mail-poller", daemon=True)
        self._stop_event = threading.Event()

    def run(self):
        log.info("메일 폴링 시작 (%s초 주기)", settings.poll_interval_sec)
        while not self._stop_event.is_set():
            try:
                sync_all()
            except Exception:
                log.exception("폴링 중 오류")
            self._stop_event.wait(settings.poll_interval_sec)

    def stop(self):
        self._stop_event.set()
