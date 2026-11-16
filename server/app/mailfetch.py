"""카페24 호스팅 메일 접속(POP3 수신, SMTP 발송)과 메일 원문 해석.

서버가 TLS 1.0 + 구형 재협상만 지원하므로 그에 맞춘 TLS 설정을 쓴다.
인증서 검증은 유지하고, POP3 는 이름 대조만 tls_name(예: pop3.cafe24.com) 기준으로 한다.
"""
import email
import poplib
import re
import smtplib
import ssl
import warnings
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

TIMEOUT = 30
MAX_BODY_CHARS = 20000
MAX_RETR_BYTES = 20 * 1024 * 1024    # 이보다 큰 메일은 앞부분만 받는다
TOP_LINES = 2000
AD_SUBJECT = re.compile(r"^\s*[\(\[]\s*광\s*고\s*[\)\]]")


def legacy_tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


# ---------- POP3 ----------

def open_tls(host: str, port: int, tls_name: str) -> poplib.POP3:
    """로그인 전 단계까지: 접속 후 STLS 로 암호화한다."""
    pop = poplib.POP3(host, port, timeout=TIMEOUT)
    try:
        pop.host = tls_name or host      # stls() 가 이 이름으로 인증서를 대조한다
        pop.stls(context=legacy_tls_context())
    except BaseException:
        pop.close()
        raise
    return pop


def connect(host: str, port: int, tls_name: str, user: str, password: str) -> poplib.POP3:
    pop = open_tls(host, port, tls_name)
    try:
        pop.user(user)
        pop.pass_(password)
    except BaseException:
        pop.close()
        raise
    return pop


def list_uids(pop: poplib.POP3) -> dict[str, int]:
    """{uid: 메시지 번호}"""
    uids = {}
    for line in pop.uidl()[1]:
        num, uid = line.decode("ascii", "replace").split(maxsplit=1)
        uids[uid] = int(num)
    return uids


def fetch_raw(pop: poplib.POP3, num: int) -> bytes:
    size = int(pop.list(num).split()[2])
    lines = pop.top(num, TOP_LINES)[1] if size > MAX_RETR_BYTES else pop.retr(num)[1]
    return b"\r\n".join(lines)


def _is_bulk(msg, subject: str) -> bool:
    """광고·대량 발송 메일인지. 발송 시스템이 붙이는 표시와 '(광고)' 제목만 본다."""
    if msg["List-Unsubscribe"] or msg["List-Id"]:
        return True
    if str(msg["Precedence"] or "").strip().lower() in ("bulk", "junk", "list"):
        return True
    return bool(AD_SUBJECT.match(subject))


def parse_mail(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else ""
        is_html = bool(part) and part.get_content_type() == "text/html"
    except Exception:
        body, is_html = "(본문을 해석하지 못했습니다)", False
    if is_html:
        body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = re.sub(r"[ \t]+", " ", body)
    body = body.strip()
    truncated = len(body) > MAX_BODY_CHARS
    try:
        attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    except Exception:
        attachments = []
    sender = str(msg["From"] or "")
    subject = str(msg["Subject"] or "(제목 없음)")
    return {
        "sender": sender,
        "recipient": str(msg["To"] or ""),
        "subject": subject,
        "mail_date": str(msg["Date"] or ""),
        "attachments": attachments,
        "body": body[:MAX_BODY_CHARS],
        "truncated": truncated,
        "message_id": str(msg["Message-ID"] or "").strip(),
        "reply_to": parseaddr(str(msg["Reply-To"] or sender))[1],
        "is_bulk": _is_bulk(msg, subject),
    }


# ---------- SMTP ----------

def open_smtp(host: str, port: int, user: str, password: str) -> smtplib.SMTP:
    smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        smtp.ehlo()
        smtp.starttls(context=legacy_tls_context())
        smtp.ehlo()
        smtp.login(user, password)
    except BaseException:
        smtp.close()
        raise
    return smtp


def check_smtp_login(host: str, port: int, user: str, password: str) -> None:
    open_smtp(host, port, user, password).quit()


def build_reply(from_addr: str, to: list[str], subject: str, body: str, in_reply_to: str = "") -> EmailMessage:
    """답장 메시지를 만든다. 제목이나 주소에 줄바꿈이 있으면 ValueError 가 난다."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


def send_mail(host: str, port: int, user: str, password: str, msg: EmailMessage, recipients: list[str]) -> None:
    """recipients 는 실제로 받는 주소 전체(숨은참조 포함)."""
    smtp = open_smtp(host, port, user, password)
    try:
        smtp.send_message(msg, from_addr=user, to_addrs=recipients)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
