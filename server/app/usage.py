"""Claude API 사용량과 환산 비용."""
from datetime import datetime, timedelta, timezone

from . import db
from .config import settings

KST = timezone(timedelta(hours=9))      # 월 경계는 한국 시간 기준

# (모델 id 앞부분, 입력 $/100만 토큰, 출력 $/100만 토큰). 위에서부터 먼저 맞는 것을 쓴다. 2026-11 기준.
PRICES = [
    ("claude-fable-5-2", 8.0, 40.0), ("claude-mythos-5-2", 8.0, 40.0),
    ("claude-fable-5", 10.0, 50.0), ("claude-mythos-5", 10.0, 50.0),
    ("claude-opus-5-5", 4.0, 20.0), ("claude-opus-5", 5.0, 25.0), ("claude-opus-4", 5.0, 25.0),
    ("claude-sonnet-5", 2.0, 10.0), ("claude-sonnet-4", 3.0, 15.0),
    ("claude-haiku-5-5", 0.10, 0.50), ("claude-haiku-4-5", 1.0, 5.0),
]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """가격표에 없는 모델이면 None."""
    for prefix, price_in, price_out in PRICES:
        if model.startswith(prefix):
            return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)
    return None


def month_key(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(KST).strftime("%Y-%m")


def month_start_utc(moment: datetime | None = None) -> str:
    local = (moment or datetime.now(timezone.utc)).astimezone(KST)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def record(conn, user, mail_id: int, result: dict) -> None:
    conn.execute(
        "INSERT INTO usage_log (user_id, username, mail_id, model, input_tokens, output_tokens,"
        " cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], user["username"], mail_id, result["model"], result["input_tokens"],
         result["output_tokens"],
         cost_usd(result["model"], result["input_tokens"], result["output_tokens"]), db.now()))


def spent_this_month(conn) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log WHERE created_at >= ?",
                       (month_start_utc(),)).fetchone()
    return float(row[0])


def budget_exceeded(conn) -> bool:
    return settings.monthly_budget_usd > 0 and spent_this_month(conn) >= settings.monthly_budget_usd


def report(conn, months: int) -> dict:
    """최근 N개월을 월별·사용자별로 묶는다."""
    rows = conn.execute("SELECT * FROM usage_log ORDER BY created_at DESC").fetchall()
    by_month: dict[str, dict[str, dict]] = {}
    for r in rows:
        key = month_key(r["created_at"])
        if key not in by_month and len(by_month) >= months:
            break
        entry = by_month.setdefault(key, {}).setdefault(
            r["username"], {"username": r["username"], "requests": 0, "input_tokens": 0,
                            "output_tokens": 0, "cost_usd": 0.0, "unpriced_requests": 0})
        entry["requests"] += 1
        entry["input_tokens"] += r["input_tokens"]
        entry["output_tokens"] += r["output_tokens"]
        if r["cost_usd"] is None:
            entry["unpriced_requests"] += 1
        else:
            entry["cost_usd"] = round(entry["cost_usd"] + r["cost_usd"], 6)
    result = []
    for key in sorted(by_month, reverse=True):
        users = sorted(by_month[key].values(), key=lambda u: -u["cost_usd"])
        result.append({"month": key, "users": users,
                       "requests": sum(u["requests"] for u in users),
                       "cost_usd": round(sum(u["cost_usd"] for u in users), 6)})
    return {"months": result, "monthly_budget_usd": settings.monthly_budget_usd,
            "spent_this_month_usd": round(spent_this_month(conn), 6)}
