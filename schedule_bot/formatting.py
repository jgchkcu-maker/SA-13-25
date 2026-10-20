from datetime import date
from html import escape


def format_cards(
    rows: list[dict], source_url: str, *, revised: bool = False, uncertain: bool = False
) -> list[str]:
    title = "Уточнение расписания" if revised else "Изменения в расписании"
    header = f"<b>{title}</b>\n<code>СА-13-25</code>"
    footer = f'\n\n<a href="{escape(source_url, quote=True)}">Публикация в «Расписание АК»</a>'
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
        if current_date != row["date"]:
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
        blocks.append((row["date"], block))
    cards, body = [], ""
    for day, block in blocks:
        if len(header + body + block + footer) > 3500:
            cards.append(header + body + footer)
            body = f"\n\n<b>{date.fromisoformat(day):%d.%m.%Y}</b>"
        body += "\n\n" + block
    if body:
        cards.append(header + body + footer)
    return cards
