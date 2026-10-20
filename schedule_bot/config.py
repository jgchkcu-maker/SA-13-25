import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

TARGET_GROUP = "СА-13-25"


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Не заполнена переменная {name}")
    return value


def positive(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} должна быть положительной")
    return value


@dataclass(frozen=True, repr=False)
class Config:
    bot_token: str
    chat_id: str
    source: str
    gemini_key: str
    model: str
    thread_id: int | None
    start_at: datetime
    settle_seconds: int

    @classmethod
    def load(cls):
        thread = os.getenv("TELEGRAM_THREAD_ID", "").strip()
        return cls(
            required("TELEGRAM_BOT_TOKEN"),
            required("TELEGRAM_CHAT_ID"),
            os.getenv("TELEGRAM_SOURCE", "@timetable_of_ak_sibsau"),
            required("GEMINI_API_KEY"),
            os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            int(thread) if thread else None,
            datetime.combine(
                date.fromisoformat(os.getenv("START_DATE", "2026-10-18")),
                time.min,
                timezone(timedelta(hours=7)),
            ),
            positive("ALBUM_SETTLE_SECONDS", 120),
        )
