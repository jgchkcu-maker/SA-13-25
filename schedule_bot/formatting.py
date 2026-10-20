from datetime import date
from html import escape

# The first card is the caption under the photos: Telegram allows 1024 characters there,
# 4096 in a plain text message. Raw HTML is counted, so the limits are conservative.
CAPTION_LIMIT = 1000
TEXT_LIMIT = 3500
CONTINUED = "\n\n<i>Продолжение — в следующем сообщении</i>"
STALE_PART = (
    "<b>Уточнение расписания · СА-13-25</b>\n\n"
    "Эта часть сообщения больше не актуальна. "
    "Актуальные данные — в первой части публикации бота."
)


def source_link(source_url: str) -> str:
    return f'\n\n<a href="{escape(source_url, quote=True)}">Публикация в «Расписание АК»</a>'


def outdated_notice(source_url: str) -> str:
    return (
        "<b>Уточнение расписания</b>\n<code>СА-13-25</code>\n\n"
        "Публикация изменена, это сообщение устарело. "
        "Актуальные изменения — в сообщении ниже." + source_link(source_url)
    )


def format_cards(
    rows: list[dict], source_url: str, *, revised: bool = False, uncertain: bool = False
) -> list[str]:
    title = "Уточнение расписания" if revised else "Изменения в расписании"
    header = f"<b>{title}</b>\n<code>СА-13-25</code>"
    footer = source_link(source_url)
    if uncertain:
        return [
            header + "\n\nИсточник отредактирован. Ранее отправленные изменения больше "
            "не удаётся подтвердить по текущей публикации.\n"
            "Проверьте оригинал перед занятиями." + footer
        ]
    blocks = []
    current_date = None
    for row in rows:
        block = ""
        starts_day = current_date != row["date"]
        if starts_day:
            current_date = row["date"]
            block += f"<b>{date.fromisoformat(current_date):%d.%m.%Y}</b>\n\n"
        block += f"<b>{escape(row['lesson'])} пара</b>\n"
        if row["kind"] == "cancelled":
            block += "Занятие отменено\n"
        block += "\n".join(
            [
                f"Предмет: {escape(row['subject'] or 'не указан / неразборчиво')}",
                f"Преподаватель: {escape(row['teacher'] or 'не указан / неразборчиво')}",
                f"Кабинет: <code>{escape(row['room'] or 'не указан / неразборчиво')}</code>",
            ]
        )
        blocks.append((row["date"], starts_day, block))
    cards, body = [], ""
    for day, starts_day, block in blocks:
        limit = CAPTION_LIMIT if not cards else TEXT_LIMIT
        if body and len(header + body + "\n\n" + block + CONTINUED + footer) > limit:
            cards.append(header + body + CONTINUED + footer)
            # Repeat the date when a day continues in the next message.
            body = "" if starts_day else f"\n\n<b>{date.fromisoformat(day):%d.%m.%Y}</b>"
        body += "\n\n" + block
    if body:
        cards.append(header + body + footer)
    return cards
