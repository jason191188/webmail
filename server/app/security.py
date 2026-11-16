"""비밀번호 해시, 세션 토큰, 메일 비밀번호 암호화, 로그인 시도 제한."""
import hashlib
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from .config import settings

_hasher = PasswordHasher()

MAX_FAILURES = 5
LOCK_SECONDS = 300
_failures: dict[str, tuple[int, float]] = {}
_failures_lock = threading.Lock()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> tuple[str, str]:
    """(클라이언트에 줄 토큰, DB 에 저장할 해시)"""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet() -> Fernet:
    return Fernet(settings.mail_enc_key.encode())


def check_enc_key() -> None:
    try:
        _fernet()
    except Exception:
        raise SystemExit(
            "MAIL_ENC_KEY 가 없거나 형식이 잘못됐습니다. 아래 명령으로 만들어 .env 에 넣으세요.\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def is_locked(key: str) -> bool:
    with _failures_lock:
        count, since = _failures.get(key, (0, 0.0))
        if count >= MAX_FAILURES and time.monotonic() - since < LOCK_SECONDS:
            return True
        if count >= MAX_FAILURES:
            _failures.pop(key, None)
        return False


def record_failure(key: str) -> None:
    with _failures_lock:
        count, _ = _failures.get(key, (0, 0.0))
        _failures[key] = (count + 1, time.monotonic())


def clear_failures(key: str) -> None:
    with _failures_lock:
        _failures.pop(key, None)
