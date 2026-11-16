"""환경변수로 받는 서버 설정."""
import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mail_enc_key: str
    admin_username: str
    admin_password: str
    claude_model: str
    claude_effort: str
    claude_fallbacks: bool
    poll_enabled: bool
    poll_interval_sec: int
    session_days: int
    vapid_public_key: str
    vapid_private_key: str
    vapid_subject: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key
                    and self.vapid_subject.startswith(("mailto:", "https://")))


def load() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        mail_enc_key=os.environ.get("MAIL_ENC_KEY", ""),
        admin_username=os.environ.get("ADMIN_USERNAME", ""),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=os.environ.get("CLAUDE_EFFORT", "medium"),
        claude_fallbacks=_bool("CLAUDE_FALLBACKS", True),
        poll_enabled=_bool("POLL_ENABLED", True),
        poll_interval_sec=int(os.environ.get("POLL_INTERVAL_SEC", "300")),
        session_days=int(os.environ.get("SESSION_DAYS", "30")),
        vapid_public_key=os.environ.get("VAPID_PUBLIC_KEY", "").strip(),
        vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
        vapid_subject=os.environ.get("VAPID_SUBJECT", "").strip(),
    )


settings = load()
