import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GROUP_MATCH_VERSION = 2


def normalize_group(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).upper().translate(str.maketrans("CA", "СА"))
    return re.sub(r"[\s\-–—−._/()]", "", value)


def is_target(value: str) -> bool:
    # The channel also uses the yearless alias for this group.
    return normalize_group(value) in {"СА1325", "СА13"}


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_text: str = Field(max_length=80, description="Exact group label visible in source")
    change_date: str = Field(description="YYYY-MM-DD, only with unambiguous date evidence")
    date_evidence: str = Field(max_length=180)
    lesson: str = Field(max_length=40, description="Pair number or explicit range, e.g. 2 or 3-4")
    subject: str | None = Field(max_length=250)
    teacher: str | None = Field(max_length=180)
    room: str | None = Field(max_length=100)
    kind: Literal["replacement", "cancelled", "other"]
    evidence: str = Field(max_length=800, description="Literal transcription of the matching row")
    image_numbers: list[int]
    certain: bool = Field(description="All non-null fields and group assignment are clear")


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["found", "not_found", "unreadable"]
    rows: list[Row] = Field(max_length=200)


def clean(value: str | None) -> str | None:
    return " ".join(value.split()) if value and value.strip() else None


def accepted_rows(result: Extraction, image_count: int) -> list[dict]:
    if result.status != "found":
        return []
    rows = {}
    for row in result.rows:
        if not row.certain or not is_target(row.group_text):
            continue
        if not row.evidence.strip() or not row.date_evidence.strip():
            continue
        # The raw group label must be present in the transcribed evidence.
        if normalize_group(row.group_text) not in normalize_group(row.evidence):
            continue
        if not row.image_numbers or any(n < 1 or n > image_count for n in row.image_numbers):
            continue
        try:
            parsed_date = date.fromisoformat(row.change_date)
        except ValueError:
            continue
        if parsed_date.isoformat() != row.change_date:
            continue
        lesson = re.sub(r"[–—−]", "-", row.lesson.strip())
        if not re.fullmatch(r"(?:[1-9]|1[0-2])(?:\s*[-,/]\s*(?:[1-9]|1[0-2]))*", lesson):
            continue
        subject, teacher, room = map(clean, (row.subject, row.teacher, row.room))
        if not any((subject, teacher, room)) and row.kind != "cancelled":
            continue
        data = dict(
            date=row.change_date,
            lesson=lesson.replace(" ", ""),
            subject=subject,
            teacher=teacher,
            room=room,
            kind=row.kind,
        )
        rows[row_key(data)] = data
    return sorted(
        rows.values(),
        key=lambda r: (
            r["date"],
            int(re.match(r"\d+", r["lesson"])[0]),
            json.dumps(r, sort_keys=True),
        ),
    )


def row_key(row: dict) -> str:
    normalized = {k: v.casefold().strip() if isinstance(v, str) else v for k, v in row.items()}
    return digest(normalized)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
