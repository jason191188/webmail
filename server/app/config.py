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
    docs_enabled: bool
    mail_retention_days: int
    backup_keep: int
    monthly_budget_usd: float

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

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
        # 메일 서버가 알려 주는 로그인 간격 제한은 10초다. 그보다 넉넉하게 1분을 기본으로 한다.
        poll_interval_sec=max(15, int(os.environ.get("POLL_INTERVAL_SEC", "60"))),
        session_days=int(os.environ.get("SESSION_DAYS", "30")),
        vapid_public_key=os.environ.get("VAPID_PUBLIC_KEY", "").strip(),
        vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
        vapid_subject=os.environ.get("VAPID_SUBJECT", "").strip(),
        docs_enabled=_bool("DOCS_ENABLED", False),
        mail_retention_days=int(os.environ.get("MAIL_RETENTION_DAYS", "90")),
        backup_keep=max(1, int(os.environ.get("BACKUP_KEEP", "7"))),
        monthly_budget_usd=float(os.environ.get("MONTHLY_BUDGET_USD", "0") or 0),
    )


settings = load()
