import argparse
import asyncio
import getpass
import logging
import os
import sys
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, utils
from telethon.sessions import StringSession

from .config import Config, required
from .engine import Engine
from .recognition import Recognizer
from .state import make_store
from .telegram import BotAPI, Source


async def create_session(output: str):
    api_id = int(os.getenv("TELEGRAM_API_ID") or input("Telegram api_id: "))
    api_hash = os.getenv("TELEGRAM_API_HASH") or getpass.getpass("Telegram api_hash: ")
    path = Path(output)
    if path.exists():
        raise ValueError("Файл уже существует; укажите другое имя")
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(
            phone=lambda: input("Номер телефона (+7...): "),
            code_callback=lambda: getpass.getpass("Код из Telegram: "),
            password=lambda: getpass.getpass("Пароль двухэтапной защиты: "),
        )
        with path.open("x", encoding="utf-8") as stream:
            stream.write(client.session.save())
        try:
            path.chmod(0o600)
        except OSError:
            pass
        print(f"Сессия сохранена в {path.resolve()}. Перенесите в GitHub Secret TELEGRAM_SESSION.")
    finally:
        await client.disconnect()


async def dialogs():
    client = TelegramClient(
        StringSession(required("TELEGRAM_SESSION")),
        int(required("TELEGRAM_API_ID")),
        required("TELEGRAM_API_HASH"),
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована")
        async for dialog in client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                print(f"{utils.get_peer_id(dialog.entity)}\t{dialog.name}")
    finally:
        await client.disconnect()


async def execute(args):
    if args.command == "session":
        await create_session(args.output)
        return
    if args.command == "dialogs":
        await dialogs()
        return
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
        store = make_store(http)
        if args.command == "init-state":
            await store.initialize()
            print("Состояние создано. Теперь можно запускать run.")
            return
        state = await store.load()
        if args.command == "resolve":
            pending = state["pending"]
            if not pending or pending["kind"] != "send":
                raise ValueError("Нет неоднозначной отправки для подтверждения")
            engine = Engine(state, store, None, None)
            if args.message_id:
                if args.message_id < 1:
                    raise ValueError("ID должен быть положительным")
                engine.complete_pending(args.message_id)
            elif args.confirm_not_delivered:
                state["pending"] = None
            else:
                raise ValueError("Укажите --message-id ID или --confirm-not-delivered")
            await store.save(state)
            print("Состояние отправки уточнено. Следующий run продолжит обработку.")
            return
        config = Config.load()
        async with Source(config) as source:
            scope = {
                "source": source.channel_id,
                "chat": config.chat_id,
                "thread": config.thread_id,
                "group": "СА-13-25",
            }
            if state["scope"] and state["scope"] != scope:
                raise RuntimeError("Источник/чат/тема изменены. Используйте отдельное состояние")
            state["scope"] = scope
            await store.save(state)
            bot = BotAPI(http, config.bot_token, config.chat_id, config.thread_id)
            recognizer = Recognizer(config.gemini_key, config.model)
            try:
                engine = Engine(state, store, bot, recognizer)
                # Resolve interrupted edits before downloading or paying for OCR.
                await engine.recover()
                with tempfile.TemporaryDirectory(prefix="ak-schedule-") as directory:
                    publications, cursor = await source.collect(state, Path(directory))
                    await engine.run(publications, cursor)
            finally:
                recognizer.close()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Изменения расписания СА-13-25")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Одна проверка канала")
    sub.add_parser("init-state", help="Однократное создание состояния, без отправки в Telegram")
    sub.add_parser("dialogs", help="Локально вывести ID каналов и групп аккаунта")
    session = sub.add_parser("session", help="Локально авторизовать аккаунт")
    session.add_argument("--output", default="telegram-session.secret")
    resolve = sub.add_parser("resolve", help="Уточнить результат прерванной отправки")
    group = resolve.add_mutually_exclusive_group(required=True)
    group.add_argument("--message-id", type=int)
    group.add_argument("--confirm-not-delivered", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Libraries may include secret-bearing URLs / extracted student data in diagnostics.
    for name in ("httpx", "httpcore", "telethon", "google_genai", "google.genai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(execute(args))
    except (ValueError, RuntimeError) as exc:
        # ValidationError contains raw model output; expose its type only.
        from pydantic import ValidationError

        print(
            "Ошибка: неверный JSON распознавания"
            if isinstance(exc, ValidationError)
            else f"Ошибка: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Ошибка: {type(exc).__name__}. Запуск остановлен; секреты скрыты.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
