import os
from dataclasses import dataclass

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
    api_id: int
    api_hash: str
    session: str
    bot_token: str
    chat_id: str
    source: str
    gemini_key: str
    model: str
    thread_id: int | None
    lookback_days: int
    settle_seconds: int

    @classmethod
    def load(cls):
        thread = os.getenv("TELEGRAM_THREAD_ID", "").strip()
        return cls(
            int(required("TELEGRAM_API_ID")),
            required("TELEGRAM_API_HASH"),
            required("TELEGRAM_SESSION"),
            required("TELEGRAM_BOT_TOKEN"),
            required("TELEGRAM_CHAT_ID"),
            required("TELEGRAM_SOURCE"),
            required("GEMINI_API_KEY"),
            os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            int(thread) if thread else None,
            positive("INITIAL_LOOKBACK_DAYS", 3),
            positive("ALBUM_SETTLE_SECONDS", 120),
        )
