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
    assert set(rows[0]) == {"id", "sender", "subject", "mail_date", "fetched_at",
                            "has_attachments", "analyzed"}


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
