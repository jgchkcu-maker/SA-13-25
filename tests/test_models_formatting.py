import pytest

from schedule_bot.formatting import format_cards
from schedule_bot.models import Extraction, Row, accepted_rows, is_target


def make_row(**kwargs):
    return Row(
        **(
            {
                "group_text": "СА-13-25",
                "change_date": "2026-10-21",
                "date_evidence": "Изменения на 21.10.2026",
                "lesson": "2",
                "subject": "Математика",
                "teacher": "Иванова А. Б.",
                "room": "204",
                "kind": "replacement",
                "evidence": "СА-13-25 2 Математика Иванова А. Б. 204",
                "image_numbers": [1],
                "certain": True,
            }
            | kwargs
        )
    )


@pytest.mark.parametrize("value", ["СА-13-25", "са 13 25", "CA-13/25", "СА1325", "(СА–13–25)"])
def test_explicit_aliases(value):
    assert is_target(value)


@pytest.mark.parametrize("value", ["СА-13", "са 13", "СА13", "CA-13", "(СА–13)", "СА.13"])
def test_short_group_alias_reaches_notification(value):
    row = make_row(group_text=value, evidence=f"{value} 2 Математика Иванова А. Б. 204")
    rows = accepted_rows(Extraction(status="found", rows=[row]), 1)
    assert is_target(value)
    assert len(rows) == 1
    assert "СА-13-25" in format_cards(rows, "https://t.me/example/1")[0]


@pytest.mark.parametrize(
    "value",
    ["СА-13-24", "СА-13/26", "СА-12-25", "СА-113-25", "СА-113", "СА-13а", "АС-13-25"],
)
def test_other_groups_rejected(value):
    assert not is_target(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"certain": False},
        {"group_text": "СА-13-24"},
        {"change_date": "2026-02-30"},
        {"change_date": "20261021"},
        {"lesson": "неясно"},
        {"date_evidence": ""},
        {"evidence": "соседняя строка"},
        {"image_numbers": [3]},
        {"image_numbers": []},
        {"subject": None, "teacher": None, "room": None},
    ],
)
def test_unsafe_rows_dropped(kwargs):
    result = Extraction(status="found", rows=[make_row(**kwargs)])
    assert accepted_rows(result, 2) == []


def test_nullable_fields_and_repeated_rows():
    row = make_row(teacher=None, room=None)
    rows = accepted_rows(Extraction(status="found", rows=[row, row]), 1)
    assert len(rows) == 1
    text = format_cards(rows, "https://t.me/example/1")[0]
    assert "не указан / неразборчиво" in text


def test_html_escape_and_length():
    rows = accepted_rows(
        Extraction(
            status="found",
            rows=[
                make_row(
                    subject="A&B <b>Test</b>",
                    teacher="X" * 180,
                    room="<10>",
                    lesson=str(i % 12 + 1),
                    change_date=f"2026-10-{i // 12 + 1:02}",
                )
                for i in range(100)
            ],
        ),
        1,
    )
    cards = format_cards(rows, "https://t.me/example/1")
    assert len(cards) > 1
    assert all(len(card) <= 3500 for card in cards)
    assert "A&amp;B &lt;b&gt;Test&lt;/b&gt;" in cards[0]
    assert "&lt;10&gt;" in cards[0]


def test_not_found_never_uses_rows():
    assert accepted_rows(Extraction(status="not_found", rows=[make_row()]), 1) == []
