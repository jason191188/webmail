"""API 동작 테스트. 메일 서버와 Claude API 는 가짜로 대체한다.

실행: (server 폴더에서) python -m pytest -q
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="webmail-test-")
os.environ["POLL_ENABLED"] = "false"
os.environ["ADMIN_USERNAME"] = "boss"
os.environ["ADMIN_PASSWORD"] = "boss-password-1"

from cryptography.fernet import Fernet  # noqa: E402

os.environ["MAIL_ENC_KEY"] = Fernet.generate_key().decode()

from app.vapidgen import b64url, generate  # noqa: E402

os.environ["VAPID_PUBLIC_KEY"], os.environ["VAPID_PRIVATE_KEY"] = generate()
os.environ["VAPID_SUBJECT"] = "mailto:admin@example.com"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import analyzer, db, mailfetch, poller, push, security  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_mail_server(monkeypatch):
    """테스트가 실제 메일 서버에 접속하지 못하게 막는다. 필요한 테스트는 각자 가짜로 바꾼다."""
    def blocked(*a, **k):
        raise AssertionError("테스트에서 실제 메일 서버에 접속하려 했습니다")

    monkeypatch.setattr(mailfetch, "connect", blocked)
    monkeypatch.setattr(mailfetch, "open_smtp", blocked)
    monkeypatch.setattr(mailfetch, "check_smtp_login", lambda *a: None)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "boss", "boss-password-1")


@pytest.fixture(scope="module")
def staff(client, admin):
    r = client.post("/admin/users", headers=admin,
                    json={"username": "staff1", "password": "staff-password-1"})
    assert r.status_code == 201 and r.json()["role"] == "user"
    return login(client, "staff1", "staff-password-1")


def add_mail(username, subject="납품 일정 문의"):
    with db.connect() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO mails (user_id, uid, sender, recipient, subject, mail_date, attachments,"
            " body, truncated, fetched_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 0, ?)",
            (uid, os.urandom(4).hex(), "kim@example.com", "me@example.com", subject,
             "Mon, 16 Nov 2026 10:00:00 +0900", "20일까지 납품 가능한가요?", db.now()))
        return cur.lastrowid


def test_web_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "메일 비서" in r.text


def test_requires_login(client):
    assert client.get("/me").status_code == 401
    assert client.get("/mails", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_wrong_password_and_lockout(client):
    for _ in range(security.MAX_FAILURES):
        assert client.post("/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401
    assert client.post("/auth/login", json={"username": "ghost", "password": "x"}).status_code == 429


def test_user_cannot_use_admin_features(client, staff):
    mail_id = add_mail("staff1")
    assert client.get("/admin/users", headers=staff).status_code == 403
    assert client.post("/admin/users", headers=staff,
                       json={"username": "x-user", "password": "password-123"}).status_code == 403
    assert client.get(f"/mails/{mail_id}", headers=staff).status_code == 403
    assert client.post(f"/mails/{mail_id}/analysis", headers=staff).status_code == 403
    assert client.get(f"/mails/{mail_id}/analysis", headers=staff).status_code == 403


def test_user_gets_alerts_only(client, staff):
    first = add_mail("staff1", "첫 번째")
    second = add_mail("staff1", "두 번째")
    rows = client.get("/mails", headers=staff, params={"after_id": first}).json()
    assert [r["id"] for r in rows] == [second]
    assert set(rows[0]) == {"id", "sender", "subject", "mail_date", "fetched_at", "has_attachments",
                            "is_bulk", "notified", "analyzed", "replied"}          # 본문은 없다


def test_mails_are_private_per_user(client, admin, staff):
    staff_mail = add_mail("staff1")
    assert staff_mail not in [r["id"] for r in client.get("/mails", headers=admin).json()]
    assert client.get(f"/mails/{staff_mail}", headers=admin).status_code == 404
    assert client.post(f"/mails/{staff_mail}/analysis", headers=admin).status_code == 404


def test_admin_analysis_on_request_only(client, admin, monkeypatch):
    calls = []

    def fake(mail):
        calls.append(mail["subject"])
        return {"summary": "납품 앞당김 문의", "actions": ["재고 확인"], "reply_needed": True,
                "reply_draft": "안녕하세요...", "model": "test-model", "input_tokens": 10, "output_tokens": 20}

    monkeypatch.setattr(analyzer, "analyze", fake)
    mail_id = add_mail("boss")

    assert calls == []                                          # 수집만으로는 Claude 를 부르지 않는다
    assert client.get(f"/mails/{mail_id}/analysis", headers=admin).status_code == 404
    r = client.post(f"/mails/{mail_id}/analysis", headers=admin)
    assert r.status_code == 200 and r.json()["actions"] == ["재고 확인"]
    assert client.post(f"/mails/{mail_id}/analysis", headers=admin).status_code == 200
    assert len(calls) == 1                                      # 두 번째 요청은 저장된 결과
    client.post(f"/mails/{mail_id}/analysis", headers=admin, params={"force": True})
    assert len(calls) == 2
    assert client.get(f"/mails/{mail_id}", headers=admin).json()["body"].startswith("20일")
    listed = {r["id"]: r for r in client.get("/mails", headers=admin).json()}
    assert listed[mail_id]["analyzed"] is True


def test_analysis_without_api_key(client, admin, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(f"/mails/{add_mail('boss')}/analysis", headers=admin)
    assert r.status_code == 503


def test_mail_account_register(client, staff, monkeypatch):
    class FakePop:
        def quit(self):
            pass

    monkeypatch.setattr(mailfetch, "connect", lambda *a: FakePop())
    monkeypatch.setattr(mailfetch, "list_uids", lambda pop: {"u1": 1, "u2": 2})
    r = client.put("/me/mail-account", headers=staff,
                   json={"mail_user": "staff1@hprobot.ai", "mail_password": "mail-secret"})
    assert r.status_code == 200 and r.json()["skipped_existing_mails"] == 2
    assert "mail_password" not in str(client.get("/me/mail-account", headers=staff).json())
    with db.connect() as conn:
        enc = conn.execute("SELECT mail_password_enc FROM mail_accounts").fetchone()[0]
    assert "mail-secret" not in enc and security.decrypt(enc) == "mail-secret"


def test_mail_account_login_failure(client, staff, monkeypatch):
    import poplib

    def boom(*a):
        raise poplib.error_proto("-ERR auth failed")

    monkeypatch.setattr(mailfetch, "connect", boom)
    r = client.put("/me/mail-account", headers=staff,
                   json={"mail_user": "staff1@hprobot.ai", "mail_password": "wrong"})
    assert r.status_code == 400


def test_webapp_files_served(client):
    assert client.get("/sw.js").headers["content-type"].startswith("text/javascript")
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "."
    for icon in ("icon-192.png", "icon-512.png"):
        assert client.get("/" + icon).content.startswith(b"\x89PNG")
    assert client.get("/nope.js").status_code == 404


SUB = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "p-key", "auth": "a-key"}}


def test_push_subscribe_test_and_cleanup(client, admin, staff, monkeypatch):
    assert client.get("/push/public-key").status_code == 401
    assert client.get("/push/public-key", headers=staff).json()["public_key"] == os.environ["VAPID_PUBLIC_KEY"]
    assert client.post("/push/test", headers=staff).status_code == 404            # 등록된 기기 없음
    bad = {"endpoint": "http://insecure.example.com/x", "keys": SUB["keys"]}
    assert client.post("/push/subscriptions", headers=staff, json=bad).status_code == 422
    assert client.post("/push/subscriptions", headers=staff, json=SUB).status_code == 204

    sent = []
    monkeypatch.setattr(push, "send_one", lambda sub, payload: sent.append((sub["endpoint"], payload)))
    assert client.post("/push/test", headers=staff).json() == {"sent": 1, "failed": 0, "devices": 1}
    assert sent[0][0] == SUB["endpoint"]
    assert client.post("/push/test", headers=admin).status_code == 404            # 다른 사용자의 기기로는 안 간다

    # 같은 기기에서 다른 사용자가 알림을 켜면 그 사용자 것으로 바뀐다
    assert client.post("/push/subscriptions", headers=admin, json=SUB).status_code == 204
    assert client.post("/push/test", headers=staff).status_code == 404
    assert client.post("/push/subscriptions", headers=staff, json=SUB).status_code == 204

    # 만료된 구독(410)은 자동으로 지운다
    monkeypatch.setattr(push, "send_one", lambda sub, payload: 410)
    assert client.post("/push/test", headers=staff).json()["failed"] == 1
    assert client.post("/push/test", headers=staff).status_code == 404

    assert client.post("/push/subscriptions", headers=staff, json=SUB).status_code == 204
    assert client.post("/push/unsubscribe", headers=staff, json={"endpoint": SUB["endpoint"]}).status_code == 204
    assert client.post("/push/test", headers=staff).status_code == 404


def test_real_push_encryption_and_request():
    """실제 라이브러리로 암호화해 로컬 가짜 푸시 서버에 보낸다 (키 형식 검증)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["headers"] = dict(self.headers)
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        device_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        sub = {"endpoint": f"http://127.0.0.1:{server.server_port}/push",
               "p256dh": b64url(device_key), "auth": b64url(os.urandom(16))}
        assert push.send_one(sub, {"title": "새 메일: 테스트", "body": "kim@example.com"}) is None
    finally:
        server.shutdown()
    headers = {k.lower(): v for k, v in received["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid ") and os.environ["VAPID_PUBLIC_KEY"] in headers["authorization"]
    assert b"kim@example.com" not in received["body"]                               # 내용은 암호화돼 나간다


def test_poller_saves_and_notifies(client, staff, monkeypatch):
    class FakePop:
        def quit(self):
            pass

    monkeypatch.setattr(mailfetch, "connect", lambda *a: FakePop())
    monkeypatch.setattr(mailfetch, "list_uids", lambda pop: {"old": 1})
    client.put("/me/mail-account", headers=staff,
               json={"mail_user": "staff1@hprobot.ai", "mail_password": "mail-secret"})

    raw = ("From: =?utf-8?b?6rmA7JiB7IiY?= <kim@example.com>\r\nSubject: =?utf-8?b?7IOIIOuplOydvA==?=\r\n"
           "Content-Type: text/plain; charset=utf-8\r\n\r\n본문입니다").encode()
    monkeypatch.setattr(mailfetch, "list_uids", lambda pop: {"old": 1, "new-1": 2})
    monkeypatch.setattr(mailfetch, "fetch_raw", lambda pop, num: raw)
    notified = []
    monkeypatch.setattr(push, "send_to_user", lambda user_id, payloads: notified.append((user_id, payloads)))

    before = client.get("/mails", headers=staff).json()
    assert client.post("/mails/sync", headers=staff).json() == {"new_mails": 1}
    after = client.get("/mails", headers=staff).json()
    assert len(after) == len(before) + 1 and after[0]["subject"] == "새 메일"
    staff_id = client.get("/me", headers=staff).json()["id"]
    assert notified == [(staff_id, [{"title": "새 메일: 새 메일", "body": "김영수 <kim@example.com>",
                                     "tag": f"mail-{after[0]['id']}"}])]
    assert client.post("/mails/sync", headers=staff).json() == {"new_mails": 0}   # 같은 메일을 두 번 알리지 않는다
    assert len(notified) == 1


def test_docs_closed_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_parse_mail_bulk_and_reply_fields():
    plain = mailfetch.parse_mail(
        b"From: Kim <kim@example.com>\r\nReply-To: sales@example.com\r\nMessage-ID: <abc@example.com>\r\n"
        b"Subject: hello\r\n\r\nbody")
    assert (plain["reply_to"], plain["message_id"], plain["is_bulk"]) == ("sales@example.com", "<abc@example.com>", False)
    assert mailfetch.parse_mail(b"From: a@b.co\r\nSubject: x\r\n\r\n.")["reply_to"] == "a@b.co"
    assert mailfetch.parse_mail(b"From: a@b.co\r\nList-Unsubscribe: <mailto:u@b.co>\r\nSubject: x\r\n\r\n.")["is_bulk"]
    assert mailfetch.parse_mail(b"From: a@b.co\r\nPrecedence: bulk\r\nSubject: x\r\n\r\n.")["is_bulk"]
    ad = "From: a@b.co\r\nSubject: =?utf-8?b?KOq0keqzoCkg7ZWg7J24?=\r\n\r\n.".encode()      # "(광고) 할인"
    assert mailfetch.parse_mail(ad)["is_bulk"]


def test_notification_rules():
    def mail(sender="Kim <kim@partner.com>", subject="견적 문의", bulk=False):
        return {"sender": sender, "subject": subject, "is_bulk": bulk}

    base = dict(push.DEFAULT_RULES)
    assert push.should_notify(mail(), base)
    assert not push.should_notify(mail(bulk=True), base)                                 # 광고는 기본으로 제외
    assert push.should_notify(mail(bulk=True), {**base, "skip_bulk": False})
    assert not push.should_notify(mail(), {**base, "block_senders": ["PARTNER.com"]})    # 대소문자 무시
    assert not push.should_notify(mail(), {**base, "block_keywords": ["견적"]})
    important = {**base, "important_senders": ["partner.com"]}
    assert push.should_notify(mail(bulk=True), important)                                # 중요 지정이 광고 제외보다 우선
    assert not push.should_notify(mail(), {**important, "block_keywords": ["견적"]})      # 차단이 가장 우선
    only = {**base, "only_important": True, "important_keywords": ["긴급"]}
    assert not push.should_notify(mail(), only)
    assert push.should_notify(mail(subject="[긴급] 납품"), only)


def test_notification_settings_api(client, staff):
    assert client.get("/me/notification-settings", headers=staff).json() == push.DEFAULT_RULES
    r = client.put("/me/notification-settings", headers=staff, json={
        "skip_bulk": False, "block_senders": [" spam@x.com ", "", "spam@x.com"], "important_keywords": ["긴급"]})
    assert r.status_code == 200
    saved = client.get("/me/notification-settings", headers=staff).json()
    assert saved["skip_bulk"] is False and saved["block_senders"] == ["spam@x.com"]
    assert saved["important_keywords"] == ["긴급"]
    too_long = client.put("/me/notification-settings", headers=staff, json={"block_keywords": ["x" * 101]})
    assert too_long.status_code == 422
    client.put("/me/notification-settings", headers=staff, json={})                      # 기본값으로 되돌림


def test_poller_respects_notification_rules(client, admin, monkeypatch):
    class FakePop:
        def quit(self):
            pass

    monkeypatch.setattr(mailfetch, "connect", lambda *a: FakePop())
    monkeypatch.setattr(mailfetch, "list_uids", lambda pop: {})
    assert client.put("/me/mail-account", headers=admin,
                      json={"mail_user": "boss@hprobot.ai", "mail_password": "pw"}).json()["smtp_error"] is None
    raws = {
        1: b"From: news@shop.com\r\nList-Unsubscribe: <mailto:u@shop.com>\r\nSubject: sale\r\n\r\n.",
        2: b"From: kim@partner.com\r\nSubject: order\r\n\r\n.",
        3: b"From: bad@spam.com\r\nSubject: hi\r\n\r\n.",
    }
    monkeypatch.setattr(mailfetch, "list_uids", lambda pop: {"n1": 1, "n2": 2, "n3": 3})
    monkeypatch.setattr(mailfetch, "fetch_raw", lambda pop, num: raws[num])
    client.put("/me/notification-settings", headers=admin, json={"block_senders": ["spam.com"]})
    pushed = []
    monkeypatch.setattr(push, "send_to_user", lambda user_id, payloads: pushed.extend(payloads))

    assert client.post("/mails/sync", headers=admin).json() == {"new_mails": 3}
    assert [p["title"] for p in pushed] == ["새 메일: order"]                              # 광고와 차단 발신자는 알리지 않는다
    rows = {r["subject"]: r for r in client.get("/mails", headers=admin).json()}
    assert (rows["sale"]["is_bulk"], rows["sale"]["notified"]) == (True, False)           # 목록에는 그대로 보인다
    assert rows["order"]["notified"] is True and rows["hi"]["notified"] is False
    client.put("/me/notification-settings", headers=admin, json={})


def test_sync_login_delay_message(client, admin, monkeypatch):
    import poplib

    def delayed(*a):
        raise poplib.error_proto(b"-ERR [LOGIN-DELAY] minimum time between mail checks violation")

    monkeypatch.setattr(mailfetch, "connect", delayed)
    assert client.post("/mails/sync", headers=admin).status_code == 429


def test_build_reply_message():
    msg = mailfetch.build_reply("me@hprobot.ai", ["kim@example.com", "lee@example.com"], "RE: 납품 일정",
                                "안녕하세요.\n확인했습니다.", "<orig@example.com>")
    assert msg["From"] == "me@hprobot.ai" and msg["To"] == "kim@example.com, lee@example.com"
    assert msg["In-Reply-To"] == "<orig@example.com>" and msg["References"] == "<orig@example.com>"
    assert msg["Message-ID"].endswith("@hprobot.ai>") and "Bcc" not in msg
    assert "확인했습니다" in msg.get_content()
    with pytest.raises(ValueError):
        mailfetch.build_reply("me@hprobot.ai", ["kim@example.com"], "제목\r\nBcc: x@evil.com", "body")


def test_reply_sending(client, admin, staff, monkeypatch):
    sent = []
    monkeypatch.setattr(mailfetch, "send_mail",
                        lambda host, port, user, pw, msg, recipients: sent.append((host, port, user, pw, msg, recipients)))
    mail_id = add_mail("boss")
    payload = {"to": ["kim@example.com"], "subject": "RE: 납품 일정 문의", "body": "확인 후 회신드리겠습니다."}

    assert client.post(f"/mails/{add_mail('staff1')}/reply", headers=staff, json=payload).status_code == 403
    assert client.post(f"/mails/{mail_id}/reply", headers=admin,
                       json={**payload, "to": ["not-an-address"]}).status_code == 422
    assert client.post(f"/mails/{mail_id}/reply", headers=admin,
                       json={**payload, "subject": "a\nBcc: x@evil.com"}).status_code == 422
    assert sent == []

    r = client.post(f"/mails/{mail_id}/reply", headers=admin, json=payload)
    assert r.status_code == 201 and r.json()["recipients"] == ["kim@example.com"]
    host, port, user, pw, msg, recipients = sent[0]
    assert (host, port, user, pw) == ("smtp.cafe24.com", 587, "boss@hprobot.ai", "pw")
    assert recipients == ["kim@example.com", "boss@hprobot.ai"]                           # 내 주소 숨은참조
    assert msg["To"] == "kim@example.com" and msg["Subject"] == "RE: 납품 일정 문의"

    client.post(f"/mails/{mail_id}/reply", headers=admin, json={**payload, "bcc_self": False})
    assert sent[1][5] == ["kim@example.com"]
    detail = client.get(f"/mails/{mail_id}", headers=admin).json()
    assert len(detail["replies"]) == 2 and detail["replies"][0]["body"] == payload["body"]
    listed = {m["id"]: m for m in client.get("/mails", headers=admin).json()}
    assert listed[mail_id]["replied"] is True

    def refuse(*a):
        import smtplib
        raise smtplib.SMTPRecipientsRefused({"kim@example.com": (550, b"no such user")})

    monkeypatch.setattr(mailfetch, "send_mail", refuse)
    assert client.post(f"/mails/{mail_id}/reply", headers=admin, json=payload).status_code == 400
    assert len(client.get(f"/mails/{mail_id}", headers=admin).json()["replies"]) == 2     # 실패는 기록하지 않는다


def test_partial_refusal_counts_as_failure(monkeypatch):
    import smtplib

    class FakeSmtp:
        def send_message(self, msg, from_addr, to_addrs):
            return {"kim@example.com": (550, b"no such user")}

        def quit(self):
            pass

    monkeypatch.setattr(mailfetch, "open_smtp", lambda *a: FakeSmtp())
    msg = mailfetch.build_reply("me@hprobot.ai", ["kim@example.com"], "s", "b")
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        mailfetch.send_mail("h", 587, "me@hprobot.ai", "pw", msg, ["kim@example.com", "me@hprobot.ai"])


def test_usage_log_report_and_budget(client, admin, staff, monkeypatch):
    import dataclasses

    from app import main, usage

    assert usage.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == 24.0
    assert usage.cost_usd("claude-haiku-5-5", 2000, 1000) == pytest.approx(0.0007)
    assert usage.cost_usd("some-future-model", 10, 10) is None

    monkeypatch.setattr(analyzer, "analyze", lambda mail: {
        "summary": "s", "actions": [], "reply_needed": False, "reply_draft": "",
        "model": "claude-opus-5-5", "input_tokens": 2000, "output_tokens": 1000})
    before = client.get("/admin/usage", headers=admin).json()
    mail_id = add_mail("boss")
    client.post(f"/mails/{mail_id}/analysis", headers=admin)
    client.post(f"/mails/{mail_id}/analysis", headers=admin)                 # 저장된 결과: 사용량 없음
    client.post(f"/mails/{mail_id}/analysis", headers=admin, params={"force": True})
    after = client.get("/admin/usage", headers=admin).json()
    assert after["spent_this_month_usd"] == pytest.approx(before["spent_this_month_usd"] + 2 * 0.028)
    month = after["months"][0]
    boss = next(u for u in month["users"] if u["username"] == "boss")
    assert month["month"] == usage.month_key(db.now()) and boss["requests"] >= 2
    assert client.get("/admin/usage", headers=staff).status_code == 403

    limited = dataclasses.replace(usage.settings, monthly_budget_usd=0.01)
    monkeypatch.setattr(usage, "settings", limited)
    monkeypatch.setattr(main, "settings", limited)
    assert client.post(f"/mails/{add_mail('boss')}/analysis", headers=admin).status_code == 429
    assert client.post(f"/mails/{mail_id}/analysis", headers=admin).status_code == 200    # 저장된 결과는 한도와 무관


def test_devices_list_and_remote_logout(client, admin):
    phone = client.post("/auth/login", headers={"User-Agent": "TestPhone/1.0"},
                        json={"username": "boss", "password": "boss-password-1"}).json()
    phone_headers = {"Authorization": "Bearer " + phone["access_token"]}
    sub = {"endpoint": "https://push.example.com/phone", "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/push/subscriptions", headers=phone_headers, json=sub).status_code == 204

    devices = client.get("/me/devices", headers=admin).json()["devices"]
    target = next(d for d in devices if d["user_agent"] == "TestPhone/1.0")
    assert target["push_enabled"] is True and target["current"] is False
    assert sum(d["current"] for d in devices) == 1

    assert client.delete("/me/devices/nope", headers=admin).status_code == 404
    assert client.delete(f"/me/devices/{target['id']}", headers=admin).status_code == 204
    assert client.get("/me", headers=phone_headers).status_code == 401                    # 그 기기는 로그아웃됨
    assert client.get("/me", headers=admin).status_code == 200
    with db.connect() as conn:                                                            # 알림 등록도 함께 지워짐
        assert conn.execute("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (sub["endpoint"],)).fetchone() is None


def test_backup_and_retention(client, admin, staff, monkeypatch):
    import dataclasses
    import sqlite3
    import time

    from app import maintenance

    assert client.get("/admin/backups", headers=staff).status_code == 403
    assert client.post("/admin/backups", headers=staff).status_code == 403

    tight = dataclasses.replace(maintenance.settings, backup_keep=2, mail_retention_days=30)
    monkeypatch.setattr(maintenance, "settings", tight)
    names = []
    for _ in range(3):
        names.append(maintenance.backup_now()["name"])
        time.sleep(1.1)                                                                   # 파일 이름이 초 단위
    kept = [b["name"] for b in maintenance.list_backups()]
    assert kept == sorted(names, reverse=True)[:2]                                        # 최근 2개만 남긴다
    copy = sqlite3.connect(tight.backup_dir / kept[0])
    assert copy.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 2                  # 백업 파일이 실제로 읽힌다
    copy.close()
    listed = client.get("/admin/backups", headers=admin).json()
    assert [b["name"] for b in listed["backups"]] == kept

    old_id, new_id = add_mail("boss", "오래된 메일"), add_mail("boss", "최근 메일")
    with db.connect() as conn:
        conn.execute("UPDATE mails SET fetched_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (old_id,))
        conn.execute("INSERT INTO sent_replies (mail_id, user_id, recipients, subject, body, sent_at)"
                     " VALUES (?, 1, '[]', 's', 'b', 't')", (old_id,))
    assert maintenance.purge_old() >= 1
    ids = [m["id"] for m in client.get("/mails", headers=admin, params={"limit": 200}).json()]
    assert new_id in ids and old_id not in ids
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM sent_replies WHERE mail_id = ?", (old_id,)).fetchone() is None

    forever = dataclasses.replace(tight, mail_retention_days=0)                           # 0 이면 지우지 않는다
    monkeypatch.setattr(maintenance, "settings", forever)
    with db.connect() as conn:
        conn.execute("UPDATE mails SET fetched_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (new_id,))
    assert maintenance.purge_old() == 0


def test_user_management_is_owner_only(client, admin, monkeypatch):
    assert client.get("/me", headers=admin).json()["is_owner"] is True
    r = client.post("/admin/users", headers=admin,
                    json={"username": "manager2", "password": "manager-password-2", "role": "admin"})
    assert r.status_code == 201 and r.json()["role"] == "admin" and r.json()["is_owner"] is False
    second = login(client, "manager2", "manager-password-2")

    # 나중에 만든 관리자는 사용자 관리를 할 수 없다
    assert client.get("/admin/users", headers=second).status_code == 403
    assert client.post("/admin/users", headers=second,
                       json={"username": "sneaky", "password": "password-123"}).status_code == 403
    assert client.patch(f"/admin/users/{r.json()['id']}", headers=second, json={"role": "user"}).status_code == 403
    owner_id = client.get("/me", headers=admin).json()["id"]
    assert client.patch(f"/admin/users/{owner_id}", headers=second, json={"password": "hacked-pass-1"}).status_code == 403

    # 하지만 관리자 기능(요약/초안)은 그대로 쓸 수 있다
    monkeypatch.setattr(analyzer, "analyze", lambda mail: {
        "summary": "s", "actions": [], "reply_needed": False, "reply_draft": "",
        "model": "test-model", "input_tokens": 1, "output_tokens": 1})
    assert client.post(f"/mails/{add_mail('manager2')}/analysis", headers=second).status_code == 200


def test_owner_migration_for_old_database(tmp_path):
    """is_owner 열이 없던 이전 버전 DB 는 가장 먼저 만든 관리자가 최초 관리자가 된다."""
    import dataclasses
    import sqlite3

    old = sqlite3.connect(tmp_path / "app.db")
    old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                " password_hash TEXT NOT NULL, role TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
    old.executemany("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, 'x', ?, 't')",
                    [("first", "admin"), ("worker", "user"), ("later", "admin")])
    old.commit()
    old.close()

    original = db.settings
    db.settings = dataclasses.replace(original, data_dir=tmp_path)
    try:
        db.init()
        db.init()                                                   # 여러 번 실행해도 결과가 같다
        with db.connect() as conn:
            owners = [r["username"] for r in conn.execute("SELECT username FROM users WHERE is_owner = 1")]
    finally:
        db.settings = original
    assert owners == ["first"]


def test_owner_guard_and_deactivation(client, admin, staff):
    me = client.get("/me", headers=admin).json()
    assert client.patch(f"/admin/users/{me['id']}", headers=admin, json={"role": "user"}).status_code == 400
    assert client.patch(f"/admin/users/{me['id']}", headers=admin, json={"is_active": False}).status_code == 400
    staff_id = client.get("/me", headers=staff).json()["id"]
    assert client.patch(f"/admin/users/{staff_id}", headers=admin, json={"is_active": False}).status_code == 200
    assert client.get("/me", headers=staff).status_code == 401
    assert client.post("/auth/login", json={"username": "staff1", "password": "staff-password-1"}).status_code == 401


def test_logout(client):
    headers = login(client, "boss", "boss-password-1")
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/me", headers=headers).status_code == 401
