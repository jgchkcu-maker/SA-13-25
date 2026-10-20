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


# Strongest first. Free tier (AI Studio, Oct 2026): 3.8/3.6 Flash — 5 RPM, 20 RPD;
# 3.5 Flash-Lite — 15 RPM, 500 RPD. Pro models are not available on the free tier.
DEFAULT_MODELS = "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite"


def models() -> tuple[str, ...]:
    pool = tuple(
        dict.fromkeys(m.strip() for m in os.getenv("GEMINI_MODELS", DEFAULT_MODELS).split(","))
    )
    pool = tuple(m for m in pool if m)
    if not pool:
        raise ValueError("GEMINI_MODELS: укажите хотя бы одну модель")
    return pool


@dataclass(frozen=True, repr=False)
class Config:
    bot_token: str
    chat_id: str
    source: str
    gemini_key: str
    models: tuple[str, ...]
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
            models(),
            int(thread) if thread else None,
            datetime.combine(
                date.fromisoformat(os.getenv("START_DATE", "2026-10-19")),
                time.min,
                timezone(timedelta(hours=7)),
            ),
            positive("ALBUM_SETTLE_SECONDS", 120),
        )
