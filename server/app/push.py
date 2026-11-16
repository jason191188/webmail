"""웹 푸시(휴대폰/브라우저 알림) 발송. VAPID 키가 설정돼 있을 때만 동작한다."""
import json
import logging

from pywebpush import WebPushException, webpush

from . import db
from .config import settings

log = logging.getLogger("push")

TTL_SEC = 12 * 3600          # 기기가 꺼져 있으면 푸시 서비스가 이 시간만큼 보관한다
GONE = (404, 410)            # 구독이 만료되거나 해제된 경우의 응답


def send_one(sub: dict, payload: dict) -> int | None:
    """구독 하나에 알림을 보낸다. 실패하면 HTTP 상태 코드(모르면 0)를, 성공하면 None 을 돌려준다."""
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"],
                               "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},   # 호출마다 새 dict (라이브러리가 내용을 바꾼다)
            ttl=TTL_SEC,
            timeout=10,
        )
        return None
    except WebPushException as e:
        return e.response.status_code if e.response is not None else 0
    except Exception as e:
        log.warning("푸시 발송 오류: %s: %s", type(e).__name__, e)
        return 0


def send_to_user(user_id: int, payloads: list[dict]) -> dict:
    """사용자의 모든 기기에 알림을 보낸다. {'sent': n, 'failed': n, 'devices': n}"""
    result = {"sent": 0, "failed": 0, "devices": 0}
    if not settings.push_enabled or not payloads:
        return result
    with db.connect() as conn:
        subs = [dict(r) for r in conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,))]
    result["devices"] = len(subs)
    for sub in subs:
        for payload in payloads:
            status = send_one(sub, payload)
            if status is None:
                result["sent"] += 1
                continue
            result["failed"] += 1
            if status in GONE:
                with db.connect() as conn:
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (sub["endpoint"],))
                break
            log.warning("푸시 발송 실패 (user %s, HTTP %s)", user_id, status)
    return result


def notify_new_mails(user_id: int, mails: list[dict]) -> None:
    """새 메일 알림. 한꺼번에 많이 오면 한 건으로 묶는다."""
    if not mails:
        return
    if len(mails) > 3:
        payloads = [{"title": f"새 메일 {len(mails)}건",
                     "body": " / ".join(m["subject"] for m in mails[:3]) + " 외", "tag": "mail-batch"}]
    else:
        payloads = [{"title": "새 메일: " + m["subject"], "body": m["sender"], "tag": f"mail-{m['id']}"}
                    for m in mails]
    try:
        send_to_user(user_id, payloads)
    except Exception:
        log.exception("새 메일 알림 중 오류")      # 알림 실패가 메일 수집을 막으면 안 된다
