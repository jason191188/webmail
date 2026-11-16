"""로그인 없이 메일 서버 접속 가능 여부와 암호화 지원만 확인한다."""
import poplib
import smtplib
import socket
import ssl
import sys

sys.stdout.reconfigure(encoding="utf-8")

POP_HOST = "webmail.hprobot.ai"
SMTP_HOST = "smtp.cafe24.com"
TIMEOUT = 15


def probe_pop3_plain(port):
    print(f"[POP3 {POP_HOST}:{port}]")
    try:
        pop = poplib.POP3(POP_HOST, port, timeout=TIMEOUT)
        print("  인사말:", pop.getwelcome().decode("utf-8", "replace"))
        try:
            caps = pop.capa()
            print("  기능:", ", ".join(sorted(caps)))
            print("  STLS(암호화 전환) 지원:", "STLS" in caps)
        except poplib.error_proto as e:
            print("  CAPA 미지원:", e)
        pop.quit()
    except Exception as e:
        print("  실패:", type(e).__name__, e)


def probe_pop3_ssl(port):
    print(f"[POP3-SSL {POP_HOST}:{port}]")
    try:
        pop = poplib.POP3_SSL(POP_HOST, port, timeout=TIMEOUT)
        print("  인사말:", pop.getwelcome().decode("utf-8", "replace"))
        pop.quit()
    except Exception as e:
        print("  실패:", type(e).__name__, e)


def probe_port(host, port, label):
    print(f"[{label} {host}:{port}]")
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            print("  포트 열림")
    except Exception as e:
        print("  실패:", type(e).__name__, e)


def probe_smtp(port):
    print(f"[SMTP {SMTP_HOST}:{port}]")
    try:
        smtp = smtplib.SMTP(SMTP_HOST, port, timeout=TIMEOUT)
        smtp.ehlo()
        print("  STARTTLS 지원:", smtp.has_extn("starttls"))
        if smtp.has_extn("starttls"):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            print("  TLS 연결 성공")
        print("  인증 방식:", smtp.esmtp_features.get("auth", "(표시 없음)"))
        smtp.quit()
    except Exception as e:
        print("  실패:", type(e).__name__, e)


probe_pop3_plain(110)
probe_pop3_ssl(995)
probe_port(POP_HOST, 143, "IMAP")
probe_port(POP_HOST, 993, "IMAP-SSL")
probe_smtp(587)
