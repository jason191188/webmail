"""카페24 호스팅 메일 서버 접속 공통 모듈.

진단 결과(2026-11-16):
- 서버가 TLS 1.0 + 구형 재협상만 지원해서 파이썬 기본 TLS 설정으로는 연결되지 않는다.
- POP3 서버(webmail.hprobot.ai) 인증서는 *.cafe24.com 이름으로 발급돼 있어,
  인증서 검증은 유지하되 이름 대조만 cafe24.com 기준으로 한다.
"""
import configparser
import poplib
import smtplib
import ssl
from pathlib import Path

POP_HOST = "webmail.hprobot.ai"
POP_PORT = 110
POP_CERT_NAME = "pop3.cafe24.com"   # 인증서(*.cafe24.com) 이름 대조용
SMTP_HOST = "smtp.cafe24.com"
SMTP_PORT = 587
TIMEOUT = 30

CONFIG_PATH = Path(__file__).with_name("config.ini")


def load_account():
    """config.ini 에서 계정 정보를 읽는다. (비밀번호는 이 파일에만 둔다)"""
    if not CONFIG_PATH.exists():
        raise SystemExit(f"{CONFIG_PATH} 가 없습니다. config.example.ini 를 복사해 채워주세요.")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(CONFIG_PATH, encoding="utf-8")
    user = cp.get("mail", "user", fallback="").strip()
    password = cp.get("mail", "password", fallback="")
    if not user or not password or "여기에" in password:
        raise SystemExit("config.ini 의 user / password 를 채워주세요.")
    return user, password


def legacy_tls_context():
    """구형 서버용 TLS 설정. 인증서 체인/이름 검증은 그대로 유지한다."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


def connect_pop3(user, password):
    pop = poplib.POP3(POP_HOST, POP_PORT, timeout=TIMEOUT)
    pop.host = POP_CERT_NAME          # stls() 가 이 이름으로 인증서를 대조한다
    pop.stls(context=legacy_tls_context())
    pop.user(user)
    pop.pass_(password)
    return pop


def connect_smtp(user, password):
    smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
    smtp.ehlo()
    smtp.starttls(context=legacy_tls_context())
    smtp.ehlo()
    smtp.login(user, password)
    return smtp
