"""MoonManager entry point for Pterodactyl, PyCharm and the terminal."""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from moonmanager.bot import Bot
from moonmanager.store import Store
from moonmanager.telegram import ApiError, Telegram, poll

ROOT = Path(__file__).resolve().parent


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    env = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="MoonManager · Менеджер групп Telegram")
    parser.add_argument("--db", type=Path, help="путь к базе SQLite")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = read_env(ROOT / ".env")
    token = (os.getenv("BOT_TOKEN") or config.get("BOT_TOKEN") or "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        print("Создайте файл .env рядом с main.py и укажите BOT_TOKEN от @BotFather.", file=sys.stderr)
        return 2
    db_path = args.db or Path(
        os.getenv("MOONMANAGER_DB") or config.get("MOONMANAGER_DB") or "data/moonmanager.sqlite3"
    )
    db_path = ROOT / db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    try:
        api = Telegram(token)
        me = api.call("getMe")
        bot = Bot(api, store, me)
        bot.configure_menu()
        logging.info("MoonManager запущен · @%s", me["username"])
        poll(api, bot)
    except KeyboardInterrupt:
        return 0
    except ApiError as exc:
        if exc.code in {401, 403, 404}:
            message = "Telegram отклонил токен. Проверьте BOT_TOKEN в файле .env."
        elif exc.code == 409:
            message = "Этот токен уже опрашивает другой процесс или привязан к webhook."
        else:
            message = "Telegram недоступен. Проверьте подключение хостинга к api.telegram.org."
        logging.error(message)
        return 1
    finally:
        store.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
