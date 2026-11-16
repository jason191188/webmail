"""메일 알림/분석 API 서버.

권한:
- 일반 사용자(user): 자기 메일 계정 등록, 새 메일 알림(보낸사람/제목/시간) 조회
- 관리자(admin): 위 기능 + 자기 메일의 본문 조회, 요약/답장 초안 요청
- 최초 관리자(is_owner): 위 기능 + 사용자 관리 (.env 로 만들어진 계정 하나뿐)
요약/답장 초안은 POST /mails/{id}/analysis 요청이 있을 때만 Claude API 를 호출한다.
"""
import json
import logging
import poplib
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import analyzer, db, mailfetch, poller, push, security
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"


def bootstrap_admin() -> None:
    """사용자가 한 명도 없을 때만 .env 의 관리자 계정을 만든다."""
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        if not settings.admin_username or len(settings.admin_password) < 8:
            raise SystemExit("첫 실행에는 ADMIN_USERNAME 과 8자 이상의 ADMIN_PASSWORD 가 필요합니다.")
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, is_owner)"
            " VALUES (?, ?, 'admin', ?, 1)",
            (settings.admin_username, security.hash_password(settings.admin_password), db.now()))
        log.info("관리자 계정 '%s' 을 만들었습니다.", settings.admin_username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    security.check_enc_key()
    db.init()
    bootstrap_admin()
    worker = None
    if settings.poll_enabled:
        worker = poller.Poller()
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="Webmail Assistant API", version="0.1.0", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


# ---------- 인증 ----------

def current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if cred is None:
        raise HTTPException(401, "로그인이 필요합니다.")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1",
            (security.hash_token(cred.credentials), db.now())).fetchone()
    if row is None:
        raise HTTPException(401, "토큰이 유효하지 않거나 만료됐습니다.")
    return row


def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "관리자만 사용할 수 있습니다.")
    return user


def owner_user(user=Depends(current_user)):
    """사용자 관리는 최초 관리자(.env 로 만들어진 계정)만 할 수 있다."""
    if not user["is_owner"]:
        raise HTTPException(403, "최초 관리자만 사용할 수 있습니다.")
    return user


def user_out(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "is_owner": bool(row["is_owner"]),
            "is_active": bool(row["is_active"]), "created_at": row["created_at"]}


class LoginIn(BaseModel):
    username: str
    password: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def web_ui():
    """웹 화면. 프록시 프리픽스 아래에서도 동작하도록 페이지가 상대 경로로 API 를 호출한다."""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# 웹앱(홈 화면 설치)용 파일. 서비스 워커는 앱과 같은 경로에 있어야 그 범위를 맡을 수 있다.
WEBAPP_FILES = {
    "sw.js": "text/javascript",
    "manifest.webmanifest": "application/manifest+json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}


def _webapp_route(name: str, media_type: str):
    def serve():
        return FileResponse(STATIC_DIR / name, media_type=media_type, headers={"Cache-Control": "no-cache"})
    app.add_api_route("/" + name, serve, methods=["GET"], include_in_schema=False)


for _name, _media_type in WEBAPP_FILES.items():
    _webapp_route(_name, _media_type)


@app.post("/auth/login")
def login(body: LoginIn):
    key = body.username.strip().lower()
    if security.is_locked(key):
        raise HTTPException(429, "로그인 실패가 반복돼 잠시 잠겼습니다. 5분 뒤 다시 시도하세요.")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
        ok = bool(row) and bool(row["is_active"]) and security.verify_password(row["password_hash"], body.password)
        if not ok:
            security.record_failure(key)
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
        security.clear_failures(key)
        token, token_hash = security.new_token()
        expires = datetime.now(timezone.utc) + timedelta(days=settings.session_days)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, row["id"], db.now(), expires.isoformat(timespec="seconds")))
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now(),))
    return {"access_token": token, "token_type": "bearer",
            "expires_at": expires.isoformat(timespec="seconds"), "user": user_out(row)}


@app.post("/auth/logout", status_code=204)
def logout(cred: HTTPAuthorizationCredentials = Depends(bearer), user=Depends(current_user)):
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (security.hash_token(cred.credentials),))


@app.get("/me")
def me(user=Depends(current_user)):
    return user_out(user)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


@app.put("/me/password", status_code=204)
def change_password(body: PasswordChange, cred: HTTPAuthorizationCredentials = Depends(bearer),
                    user=Depends(current_user)):
    if not security.verify_password(user["password_hash"], body.current_password):
        raise HTTPException(400, "현재 비밀번호가 올바르지 않습니다.")
    with db.connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (security.hash_password(body.new_password), user["id"]))
        # 지금 쓰는 세션만 남기고 나머지 기기는 로그아웃시킨다
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                     (user["id"], security.hash_token(cred.credentials)))


# ---------- 관리자: 사용자 관리 ----------

class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(min_length=8, max_length=200)
    role: Literal["admin", "user"] = "user"


class UserPatch(BaseModel):
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


@app.get("/admin/users")
def list_users(_=Depends(owner_user)):
    with db.connect() as conn:
        return [user_out(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


@app.post("/admin/users", status_code=201)
def create_user(body: UserCreate, _=Depends(owner_user)):
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone():
            raise HTTPException(409, "이미 있는 아이디입니다.")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (body.username, security.hash_password(body.password), body.role, db.now()))
        return user_out(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.patch("/admin/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, _=Depends(owner_user)):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "사용자를 찾을 수 없습니다.")
        role = body.role or row["role"]
        active = row["is_active"] if body.is_active is None else int(body.is_active)
        if row["is_owner"] and (role != "admin" or not active):
            raise HTTPException(400, "최초 관리자는 강등하거나 비활성화할 수 없습니다.")
        conn.execute("UPDATE users SET role = ?, is_active = ? WHERE id = ?", (role, active, user_id))
        if body.password:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (security.hash_password(body.password), user_id))
        if body.password or not active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return user_out(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


# ---------- 메일 계정 ----------

class MailAccountIn(BaseModel):
    mail_user: str = Field(min_length=1, max_length=200)
    mail_password: str = Field(min_length=1, max_length=200)
    pop_host: str = Field(default="webmail.hprobot.ai", min_length=1, max_length=200)
    pop_port: int = Field(default=110, ge=1, le=65535)
    tls_name: str = Field(default="pop3.cafe24.com", max_length=200,
                          description="서버 인증서를 대조할 이름. 비우면 pop_host 를 쓴다.")


def account_out(row) -> dict:
    return {"mail_user": row["mail_user"], "pop_host": row["pop_host"], "pop_port": row["pop_port"],
            "tls_name": row["tls_name"], "last_checked_at": row["last_checked_at"],
            "last_error": row["last_error"]}


@app.get("/me/mail-account")
def get_mail_account(user=Depends(current_user)):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM mail_accounts WHERE user_id = ?", (user["id"],)).fetchone()
    if row is None:
        raise HTTPException(404, "등록된 메일 계정이 없습니다.")
    return account_out(row)


@app.put("/me/mail-account")
def put_mail_account(body: MailAccountIn, user=Depends(current_user)):
    """메일 서버에 실제로 로그인해 본 뒤 저장한다. 이미 있던 메일은 알림 대상에서 제외한다."""
    try:
        pop = mailfetch.connect(body.pop_host, body.pop_port, body.tls_name,
                                body.mail_user, body.mail_password)
    except poplib.error_proto:
        raise HTTPException(400, "메일 서버 로그인에 실패했습니다. 계정과 비밀번호를 확인하세요.")
    except ssl.SSLError as e:
        raise HTTPException(400, f"메일 서버와 암호화 연결에 실패했습니다: {e.reason or e}")
    except OSError as e:
        raise HTTPException(400, f"메일 서버에 접속하지 못했습니다: {e}")
    try:
        existing = list(mailfetch.list_uids(pop))
    finally:
        try:
            pop.quit()
        except Exception:
            pass
    with db.connect() as conn:
        conn.execute("DELETE FROM mail_accounts WHERE user_id = ?", (user["id"],))
        cur = conn.execute(
            "INSERT INTO mail_accounts (user_id, pop_host, pop_port, tls_name, mail_user,"
            " mail_password_enc, last_checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], body.pop_host, body.pop_port, body.tls_name, body.mail_user,
             security.encrypt(body.mail_password), db.now()))
        conn.executemany("INSERT INTO pop_uids (account_id, uid) VALUES (?, ?)",
                         [(cur.lastrowid, uid) for uid in existing])
        row = conn.execute("SELECT * FROM mail_accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {**account_out(row), "skipped_existing_mails": len(existing)}


@app.delete("/me/mail-account", status_code=204)
def delete_mail_account(user=Depends(current_user)):
    with db.connect() as conn:
        conn.execute("DELETE FROM mail_accounts WHERE user_id = ?", (user["id"],))


# ---------- 푸시 알림 (휴대폰 / 브라우저) ----------

class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=300)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000, pattern=r"^https://")
    keys: PushKeys


class PushEndpointIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000)


def require_push():
    if not settings.push_enabled:
        raise HTTPException(503, "서버에 푸시 알림 설정(VAPID 키)이 없습니다.")


@app.get("/push/public-key")
def push_public_key(_=Depends(current_user)):
    require_push()
    return {"public_key": settings.vapid_public_key}


@app.post("/push/subscriptions", status_code=204)
def push_subscribe(body: PushSubscriptionIn, user=Depends(current_user)):
    """이 기기를 알림 대상으로 등록한다. 같은 기기를 다른 사용자가 쓰면 그 사용자 것으로 바뀐다."""
    require_push()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET"
            " user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth",
            (body.endpoint, user["id"], body.keys.p256dh, body.keys.auth, db.now()))


@app.post("/push/unsubscribe", status_code=204)
def push_unsubscribe(body: PushEndpointIn, user=Depends(current_user)):
    with db.connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
                     (body.endpoint, user["id"]))


@app.post("/push/test")
def push_test(user=Depends(current_user)):
    """내 기기들에 테스트 알림을 보낸다."""
    require_push()
    result = push.send_to_user(user["id"], [{"title": "테스트 알림", "body": "메일 비서 알림이 정상적으로 도착했습니다.",
                                             "tag": "test"}])
    if result["devices"] == 0:
        raise HTTPException(404, "알림을 켠 기기가 없습니다.")
    return result


# ---------- 메일 알림 ----------

def mail_summary_out(row) -> dict:
    return {"id": row["id"], "sender": row["sender"], "subject": row["subject"],
            "mail_date": row["mail_date"], "fetched_at": row["fetched_at"],
            "has_attachments": row["attachments"] != "[]", "analyzed": bool(row["analyzed"])}


@app.get("/mails")
def list_mails(after_id: int | None = Query(default=None, ge=0, description="이 id 보다 새 메일만 (앱의 알림 폴링용)"),
               limit: int = Query(default=50, ge=1, le=200), user=Depends(current_user)):
    """새 메일 알림 목록. after_id 를 주면 그 뒤에 도착한 메일을 오래된 순으로, 없으면 최신순으로 준다."""
    base = ("SELECT m.*, EXISTS (SELECT 1 FROM analyses a WHERE a.mail_id = m.id) AS analyzed"
            " FROM mails m WHERE m.user_id = ?")
    with db.connect() as conn:
        if after_id is None:
            rows = conn.execute(base + " ORDER BY m.id DESC LIMIT ?", (user["id"], limit)).fetchall()
        else:
            rows = conn.execute(base + " AND m.id > ? ORDER BY m.id ASC LIMIT ?",
                                (user["id"], after_id, limit)).fetchall()
    return [mail_summary_out(r) for r in rows]


@app.post("/mails/sync")
def sync_now(user=Depends(current_user)):
    """주기를 기다리지 않고 지금 메일 서버를 확인한다."""
    with db.connect() as conn:
        acc = conn.execute("SELECT id FROM mail_accounts WHERE user_id = ?", (user["id"],)).fetchone()
    if acc is None:
        raise HTTPException(404, "등록된 메일 계정이 없습니다.")
    try:
        return {"new_mails": poller.sync_account(acc["id"])}
    except Exception as e:
        raise HTTPException(502, f"메일 서버 확인에 실패했습니다: {type(e).__name__}")


def own_mail(mail_id: int, user) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM mails WHERE id = ? AND user_id = ?",
                           (mail_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "메일을 찾을 수 없습니다.")
    mail = dict(row)
    mail["attachments"] = json.loads(mail["attachments"])
    mail["truncated"] = bool(mail["truncated"])
    return mail


@app.get("/mails/{mail_id}")
def get_mail(mail_id: int, user=Depends(admin_user)):
    mail = own_mail(mail_id, user)
    return {k: mail[k] for k in ("id", "sender", "recipient", "subject", "mail_date",
                                 "attachments", "body", "truncated", "fetched_at")}


# ---------- 관리자: 요약 / 답장 초안 ----------

def analysis_out(row) -> dict:
    return {"mail_id": row["mail_id"], "summary": row["summary"], "actions": json.loads(row["actions"]),
            "reply_needed": bool(row["reply_needed"]), "reply_draft": row["reply_draft"],
            "model": row["model"], "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"], "created_at": row["created_at"]}


@app.get("/mails/{mail_id}/analysis")
def get_analysis(mail_id: int, user=Depends(admin_user)):
    own_mail(mail_id, user)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE mail_id = ?", (mail_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "아직 분석을 요청하지 않은 메일입니다.")
    return analysis_out(row)


@app.post("/mails/{mail_id}/analysis")
def request_analysis(mail_id: int,
                     force: bool = Query(default=False, description="true 면 저장된 결과가 있어도 다시 분석한다"),
                     user=Depends(admin_user)):
    """Claude API 로 요약과 답장 초안을 만든다. 이미 분석한 메일은 저장된 결과를 돌려준다(추가 과금 없음)."""
    mail = own_mail(mail_id, user)
    if not force:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM analyses WHERE mail_id = ?", (mail_id,)).fetchone()
        if row is not None:
            return analysis_out(row)
    try:
        result = analyzer.analyze(mail)
    except analyzer.AnalyzerError as e:
        raise HTTPException(e.status, e.message)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analyses (mail_id, summary, actions, reply_needed, reply_draft,"
            " model, input_tokens, output_tokens, requested_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mail_id, result["summary"], json.dumps(result["actions"], ensure_ascii=False),
             int(result["reply_needed"]), result["reply_draft"], result["model"],
             result["input_tokens"], result["output_tokens"], user["id"], db.now()))
        row = conn.execute("SELECT * FROM analyses WHERE mail_id = ?", (mail_id,)).fetchone()
    return analysis_out(row)
