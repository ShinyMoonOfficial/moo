"""Telegram commands and event handling for MoonManager."""

import logging
import re
import unicodedata
from datetime import datetime, timezone

from .filters import Flood, contains_word, is_link, normalized
from .moderation import (
    DELETE, RESTRICT, SETTINGS, Sanctions, UserError, human_duration, is_admin,
    need_right, parse_duration, present,
)
from .telegram import ApiError

LOG = logging.getLogger(__name__)
GROUP = {"group", "supergroup"}
COMMAND = re.compile(r"/([a-z_]{1,32})(?:@([a-z0-9_]+))?", re.IGNORECASE | re.ASCII)
PUBLIC = {"help", "start", "rules", "profile", "id", "top", "rep", "report", "warns"}
MEMBER_COMMANDS = {"warn", "unwarn", "mute", "unmute", "ban", "unban", "kick"}
ADMIN = MEMBER_COMMANDS | {
    "delete", "check", "config", "filter", "setrules", "setwelcome", "reports", "resolve", "modlog",
}
MOD_HELP = (
    "🌙 MoonManager · команды модерации\n\n"
    "Ответь на сообщение участника или укажи его числовой ID.\n"
    "/warn причина · /unwarn\n"
    "/mute 15m причина · /unmute\n"
    "/ban 2h причина · /ban forever причина · /unban ID\n"
    "/kick причина · /delete (ответом на сообщение)\n\n"
    "В супергруппах Telegram при бане и кике может удалить прежние сообщения участника.\n\n"
    "/check — проверка прав бота\n"
    "/config — состояние настроек\n"
    "/filter — список стоп-слов и управление ими\n"
    "/setrules текст · /setwelcome текст\n"
    "/reports и /modlog — отправить список в личку\n"
    "/resolve ID — закрыть жалобу\n\n"
    "Команды выполняются от личного профиля администратора."
)
HELP = (
    "🌙 MoonManager\n\n"
    "/rules — правила группы\n"
    "/profile — профиль по своей активности или по ответу\n"
    "/top — активность · /top rep — репутация\n"
    "/rep — поблагодарить участника, ответив на сообщение\n"
    "/report причина — пожаловаться ответом на сообщение\n"
    "/warns — число активных предупреждений\n"
    "/id — свой Telegram ID или ID по ответу\n"
    "/modhelp — команды администрации"
)
MENU = {
    "help": "Команды MoonManager", "rules": "Правила группы",
    "profile": "Активность и репутация", "top": "Рейтинг участников",
    "rep": "Поблагодарить ответом на сообщение", "report": "Пожаловаться на сообщение",
    "warns": "Число предупреждений", "id": "Telegram ID", "modhelp": "Команды модерации",
}
TOGGLES = {"welcome", "flood", "links"}
TOGGLE_RIGHTS = {"welcome": SETTINGS, "flood": DELETE, "links": DELETE}
CONTENT = {
    "text", "caption", "photo", "video", "animation", "audio", "voice", "sticker",
    "document", "video_note", "poll", "contact", "location", "venue", "dice",
    "game", "paid_media", "story", "checklist", "rich_message",
}


def short(text: str, size: int) -> str:
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C") or char.isspace())
    return " ".join(text.split())[:size]


def label(user: dict) -> str:
    return short(" ".join([user.get("first_name", ""), user.get("last_name", "")]), 44) or f"ID {user['id']}"


def message_link(chat: dict, message: int) -> str:
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message}"
    if chat["type"] == "supergroup" and str(chat["id"]).startswith("-100"):
        return f"https://t.me/c/{str(chat['id'])[4:]}/{message}"
    return ""


def time_label(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%d.%m %H:%M UTC")


def split_first(value: str) -> tuple[str, str]:
    parts = value.split(maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")


def user_id(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,15}", value) and int(value) < 2 ** 52:
        return int(value)
    raise UserError("Ответьте на сообщение участника или укажите его числовой ID после команды.")


class Bot:
    def __init__(self, api, store, me: dict):
        self.api = api
        self.store = store
        self.username = me["username"]
        self.bot_id = me["id"]
        self.flood = Flood()
        self.sanctions = Sanctions(api, store)

    def configure_menu(self) -> None:
        for scope, commands in (
            ("all_private_chats", {"start": "Открыть MoonManager", "help": "Помощь"}),
            ("all_group_chats", MENU),
        ):
            try:
                self.api.call("setMyCommands", scope={"type": scope}, commands=[
                    {"command": command, "description": description} for command, description in commands.items()
                ])
            except ApiError as exc:
                LOG.warning("Не удалось обновить меню команд (код %s).", exc.code)

    def parse_command(self, text: str) -> tuple[str, str]:
        token, args = split_first(text)
        if match := COMMAND.fullmatch(token):
            command, username = match.groups()
            if username is None or username.casefold() == self.username.casefold():
                return command.casefold(), args
        if text.casefold().strip() == "+реп":
            return "rep", ""
        return "", ""

    def reply(self, message: dict, text: str) -> None:
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None
        try:
            self.api.send(message["chat"]["id"], text, thread=thread)
        except ApiError as exc:
            LOG.warning("Ответ в чате не доставлен (код %s).", exc.code)

    def handle(self, update: dict, now: int) -> None:
        edited = "edited_message" in update
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message["chat"]
        chat_id = chat["id"]
        if new := message.get("migrate_to_chat_id"):
            self.store.migrate(chat_id, new)
            return
        if old := message.get("migrate_from_chat_id"):
            self.store.migrate(old, chat_id)
            return
        if self.store.old_chat(chat_id):
            return
        event_time = message.get("edit_date" if edited else "date", now)
        if now - event_time > 300:
            return
        actor = message.get("from")
        command, args = self.parse_command(message.get("text", ""))
        if chat["type"] == "private" and not edited and actor and not actor.get("is_bot"):
            if command in {"start", "help"}:
                self.reply(
                    message, "🌙 MoonManager · помощник для групп Telegram.\n"
                    f"Добавить в группу: https://t.me/{self.username}?startgroup=moonmanager\n\n"
                    "В группе доступны /help и /modhelp. Сюда бот присылает жалобы и журнал по запросу администратора."
                )
            return
        if chat["type"] not in GROUP:
            return

        options = self.store.options(chat_id)
        if not edited and (newcomers := message.get("new_chat_members")):
            self.welcome(message, newcomers, options)
            return
        if message.get("sender_chat"):
            if command in ADMIN and message["sender_chat"]["id"] == chat_id:
                self.reply(message, "Отправьте команду от своего личного профиля администратора.")
            return
        if not actor or actor.get("is_bot") or not CONTENT.intersection(message):
            return
        actor_id = actor["id"]
        actor_name = label(actor)

        if self.filter_message(message, options, now, edited):
            return
        if edited:
            return
        self.store.remember(chat_id, actor_id, actor_name,
                            count=not command and not message.get("text", "").startswith("/"))
        try:
            if command in PUBLIC:
                self.public(message, command, args, options, now)
            elif command == "modhelp":
                self.reply(message, MOD_HELP)
            elif command in ADMIN:
                admin = self.api.member(chat_id, actor_id)
                need_right(admin)
                self.admin(message, command, args, admin, options, now)
        except UserError as exc:
            self.reply(message, str(exc))
        except ApiError as exc:
            LOG.warning("Действие Telegram не подтверждено (код %s).", exc.code)
            self.reply(message, "Telegram не подтвердил действие. Проверьте права бота и состояние участника.")

    def welcome(self, message: dict, members: list[dict], options) -> None:
        chat = message["chat"]
        names = []
        for member in members:
            if not member.get("is_bot"):
                name = label(member)
                self.store.remember(chat["id"], member["id"], name)
                names.append(name)
        if options.welcome and names:
            shown = ", ".join(names[:10])
            if len(names) > 10:
                shown += f" и ещё {len(names) - 10}"
            values = {"name": shown, "chat": short(chat.get("title", ""), 64)}
            # Limit expansion when a custom greeting repeats placeholders.
            greeting = re.sub(r"\{(name|chat)\}", lambda match: values[match[1]],
                              options.welcome_text)[:1600]
            self.reply(message, greeting)

    def filter_message(self, message: dict, options, now: int, edited: bool) -> bool:
        if not (options.flood or options.links or options.words):
            return False
        chat = message["chat"]["id"]
        actor = message["from"]
        pid = actor["id"]
        text = message.get("text", message.get("caption", ""))
        entities = message.get("entities", message.get("caption_entities", []))
        flood = options.flood and not edited and self.flood.too_many(chat, pid, now, message, text)
        reasons = {
            "ссылка": options.links and is_link(text, entities),
            "стоп-слово": options.words and contains_word(text, options.words),
            "флуд": flood,
        }
        reason = next((name for name, matched in reasons.items() if matched), "")
        if not reason:
            return False
        try:
            # Check the sender at the moment of removal so a newly promoted admin is exempt.
            member = self.api.member(chat, pid)
            if is_admin(member) or not present(member):
                return False
            self.api.call("deleteMessage", chat_id=chat, message_id=message["message_id"])
        except ApiError as exc:
            if self.flood.should_notify(chat, pid, now):
                LOG.warning("Фильтр не смог удалить сообщение (код %s).", exc.code)
            return False
        self.store.log(chat, "Фильтр", "MoonManager", label(actor), reason, now)
        if self.flood.should_notify(chat, pid, now):
            self.reply(message, f"🛡 {label(actor)}, сообщение удалено · {reason}. /rules")
        return True

    def from_reply(self, message: dict) -> dict:
        original = message.get("reply_to_message")
        if not original or original.get("sender_chat") or not original.get("from"):
            raise UserError("Ответьте на сообщение участника этой группы.")
        return original

    def public(self, message: dict, command: str, args: str, options, now: int) -> None:
        chat = message["chat"]
        chat_id = chat["id"]
        actor = message["from"]
        pid = actor["id"]
        if command in {"help", "start"}:
            self.reply(message, HELP)
        elif command == "rules":
            self.reply(message, f"📜 Правила группы\n\n{options.rules}" if options.rules else "Правила пока не заданы.")
        elif command == "top":
            by_reputation = args.casefold() == "rep"
            if args and not by_reputation:
                raise UserError("Используйте /top для активности или /top rep для репутации.")
            rows = self.store.leaderboard(chat_id, by_reputation)
            title = "🌟 Репутация" if by_reputation else "💬 Активность"
            lines = [f"{i}. {row['name']} · {row['score']}" for i, row in enumerate(rows, 1)]
            self.reply(message, title + "\n\n" + ("\n".join(lines) if lines else "Рейтинг пока пуст."))
        elif command in {"profile", "warns", "id"}:
            target = self.from_reply(message)["from"] if message.get("reply_to_message") else actor
            if command == "id":
                self.reply(message, f"{label(target)} · ID {target['id']}")
            elif command == "warns":
                count = self.store.warning_count(chat_id, target["id"])
                self.reply(message, f"⚠️ {label(target)} · активных предупреждений {count}")
            else:
                posts, score = self.store.profile(chat_id, target["id"])
                self.reply(message, f"👤 {label(target)}\nСообщений · {posts}\nРепутация · {score}")
        elif command in {"rep", "report"}:
            original = self.from_reply(message)
            target = original["from"]
            if target["id"] == pid or target.get("is_bot"):
                raise UserError("Выберите другого участника группы.")
            self.store.remember(chat_id, target["id"], label(target))
            if command == "rep":
                if not self.store.commend(chat_id, pid, target["id"], now):
                    raise UserError("За 24 часа можно дать до пяти плюсов, по одному каждому участнику.")
                self.reply(message, f"🌟 {label(target)} получает +1 к репутации.")
            else:
                reason = short(args, 200) or "Без уточнения"
                report_id = self.store.report(
                    chat_id, pid, target["id"], original["message_id"],
                    message_link(chat, original["message_id"]), reason, now,
                )
                if report_id is None:
                    raise UserError("Это сообщение уже было отмечено вами или ещё не прошла минута с предыдущей жалобы.")
                self.reply(message, f"📝 Жалоба #{report_id} записана для администрации.")

    def admin(self, message: dict, command: str, args: str, admin: dict, options, now: int) -> None:
        chat = message["chat"]["id"]
        actor = message["from"]
        if command in MEMBER_COMMANDS:
            need_right(admin, RESTRICT)
            self.for_member(message, command, args, options, now)
        elif command == "delete":
            need_right(admin, DELETE)
            original = message.get("reply_to_message")
            if not original:
                raise UserError("Ответьте командой /delete на сообщение, которое хотите удалить.")
            self.sanctions.bot_can(chat, self.bot_id, DELETE)
            self.api.call("deleteMessage", chat_id=chat, message_id=original["message_id"])
            self.store.log(chat, "Удаление", label(actor), f"сообщение {original['message_id']}", "", now)
            self.reply(message, "🗑 Сообщение удалено.")
        elif command == "check":
            bot = self.api.member(chat, self.bot_id)
            admin_flag = is_admin(bot)
            group_type = "супергруппа" if message["chat"]["type"] == "supergroup" else "обычная группа"
            self.reply(message, "🌙 MoonManager · проверка прав\n"
                       f"Тип · {group_type}\nАдминистратор · {'да' if admin_flag else 'нет'}\n"
                       f"Удаление сообщений · {'да' if admin_flag and bot.get(DELETE) else 'нет'}\n"
                       f"Блокировка пользователей · {'да' if admin_flag and bot.get(RESTRICT) else 'нет'}")
        elif command == "config":
            self.configure(message, admin, options, args, now)
        elif command == "filter":
            self.word_list(message, admin, options, args, now)
        elif command in {"setrules", "setwelcome"}:
            need_right(admin, SETTINGS)
            self.set_text(message, command, options, args, now)
        elif command == "reports":
            lines = [
                f"#{row['id']} · {time_label(row['created'])}\n"
                f"От {self.store.name(chat, row['reporter'])} на {self.store.name(chat, row['subject'])}\n"
                f"{row['reason']}" + (f"\n{row['src_link']}" if row["src_link"] else "")
                for row in self.store.reports(chat)
            ]
            self.private_list(message, "📝 Открытые жалобы", lines)
        elif command == "resolve":
            if not re.fullmatch(r"[1-9][0-9]{0,17}", args):
                raise UserError("Укажите номер жалобы. Пример: /resolve 12")
            if not self.store.resolve(chat, int(args), now):
                raise UserError("Активная жалоба с таким номером здесь не найдена.")
            self.store.log(chat, "Жалоба закрыта", label(actor), f"#{args}", "", now)
            self.reply(message, f"Жалоба #{args} закрыта.")
        elif command == "modlog":
            lines = [
                f"{time_label(row['created'])} · {row['kind']} · {row['subject']}\n"
                f"{row['actor']}" + (f" · {row['note']}" if row["note"] else "")
                for row in self.store.log_rows(chat)
            ]
            self.private_list(message, "📋 Последние действия модерации", lines)

    def for_member(self, message: dict, command: str, args: str, options, now: int) -> None:
        chat = message["chat"]["id"]
        actor = message["from"]
        if message.get("reply_to_message"):
            uid = self.from_reply(message)["from"]["id"]
        else:
            value, args = split_first(args)
            uid = user_id(value)
        if uid == actor["id"]:
            raise UserError("Выберите другого участника.")
        member = self.api.member(chat, uid)
        user = member["user"]
        if user.get("is_bot") or is_admin(member):
            raise UserError("Санкции для ботов и администраторов недоступны в MoonManager.")
        name = label(user)
        self.store.remember(chat, uid, name)
        note = short(args, 160)
        if command in {"warn", "unwarn"}:
            self.warning(message, member, command, options, name, note, now)
            return
        if message["chat"]["type"] != "supergroup":
            raise UserError("Для команды нужна супергруппа Telegram.")
        self.sanctions.bot_can(chat, self.bot_id, RESTRICT)
        if command in {"mute", "ban"}:
            first, reason = split_first(args)
            duration = 0
            if command == "mute" or first.casefold() != "forever":
                duration = parse_duration(first or ("10m" if command == "mute" else ""))
            note = short(reason, 160)
            if command == "mute":
                self.sanctions.mute(chat, uid, member, now, duration)
                action = "Мут"
            else:
                if not present(member):
                    raise UserError("Для бана выберите участника, который сейчас в группе.")
                self.sanctions.ban(chat, uid, now + duration if duration else None)
                action = "Бан"
            period = human_duration(duration) if duration else "бессрочно"
            note = f"{period}" + (f" · {note}" if note else "")
        elif command == "unmute":
            self.sanctions.unmute(chat, uid, member, now)
            action = "Мут снят"
        elif command == "unban":
            if member["status"] != "kicked":
                raise UserError("Участник сейчас не заблокирован.")
            self.sanctions.unban(chat, uid)
            action = "Бан снят"
        else:
            if not present(member):
                raise UserError("Этот участник сейчас вне группы.")
            can_return = self.sanctions.kick(chat, uid, now)
            action = "Исключение" if can_return else "Исключение · бан истечёт через 75 секунд"
        self.store.log(chat, action, label(actor), name, note, now)
        self.reply(message, f"🛡 {action} · {name}" + (f" · {note}" if note else ""))

    def warning(self, message: dict, member: dict, command: str, options, name: str,
                note: str, now: int) -> None:
        chat = message["chat"]["id"]
        actor = label(message["from"])
        uid = member["user"]["id"]
        if command == "unwarn":
            if not self.store.unwarn(chat, uid):
                raise UserError("У участника нет активных предупреждений.")
            self.store.log(chat, "Предупреждение снято", actor, name, note, now)
            self.reply(message, f"Предупреждение снято · {name} · осталось {self.store.warning_count(chat, uid)}.")
            return
        if not present(member):
            raise UserError("Предупреждения выдаются участникам группы.")
        count = self.store.warn(chat, uid, now)
        self.store.log(chat, "Предупреждение", actor, name, note, now)
        alert = f"⚠️ {name} · предупреждений {count}/{options.warn_limit}"
        if note:
            alert += f" · {note}"
        if options.warn_mute and count % options.warn_limit == 0:
            try:
                if message["chat"]["type"] != "supergroup":
                    raise UserError("Автомуту нужна супергруппа Telegram.")
                self.sanctions.bot_can(chat, self.bot_id, RESTRICT)
                self.sanctions.mute(chat, uid, member, now, options.warn_mute)
            except (UserError, ApiError):
                alert += "\nПредупреждение сохранено. Автомут применить не удалось."
            else:
                period = human_duration(options.warn_mute)
                self.store.log(chat, "Автомут", "MoonManager", name, period, now)
                alert += f"\n🔇 Мут за предупреждения · {period}"
        self.reply(message, alert)

    def configure(self, message: dict, admin: dict, options, args: str, now: int) -> None:
        chat = message["chat"]["id"]
        key, value = split_first(args.casefold())
        if not key:
            text = "\n".join((
                f"Приветствия · {'вкл' if options.welcome else 'выкл'}",
                f"Антифлуд · {'вкл' if options.flood else 'выкл'}",
                f"Фильтр ссылок · {'вкл' if options.links else 'выкл'}",
                f"Стоп-слов · {len(options.words)}",
                f"Лимит предупреждений · {options.warn_limit}",
                f"Автомут · {human_duration(options.warn_mute) if options.warn_mute else 'выкл'}",
            ))
            self.reply(message, f"⚙️ Настройки MoonManager\n{text}\n\n"
                       "/config welcome|flood|links on|off\n/config warnlimit 3\n/config warnmute 10m (0 — выкл)")
            return
        if key in TOGGLES and value in {"on", "off"}:
            right = TOGGLE_RIGHTS[key]
            need_right(admin, right)
            if value == "on" and right == DELETE:
                self.sanctions.bot_can(chat, self.bot_id, DELETE)
            setattr(options, key, value == "on")
        elif key == "warnlimit" and re.fullmatch(r"[1-9]|1[0-9]|20", value):
            need_right(admin, RESTRICT)
            options.warn_limit = int(value)
        elif key == "warnmute" and value:
            need_right(admin, RESTRICT)
            duration = 0 if value == "0" else parse_duration(value)
            if duration:
                if message["chat"]["type"] != "supergroup":
                    raise UserError("Для автомутов нужна супергруппа Telegram.")
                self.sanctions.bot_can(chat, self.bot_id, RESTRICT)
            options.warn_mute = duration
        else:
            raise UserError("Параметр или значение неизвестны. Список доступен командой /config.")
        self.store.save_options(chat, options)
        self.store.log(chat, "Настройки", label(message["from"]), key, value, now)
        self.reply(message, f"⚙️ Сохранено · {key} {value}")

    def word_list(self, message: dict, admin: dict, options, args: str, now: int) -> None:
        chat = message["chat"]["id"]
        operation, value = split_first(args)
        if not operation or operation == "list":
            text = ", ".join(options.words) or "Фильтр слов пуст."
            self.reply(message, "Стоп-слова\n" + text + "\n\n/filter add фраза · /filter remove фраза")
            return
        need_right(admin, DELETE)
        word = normalized(value)
        if operation not in {"add", "remove"} or not 2 <= len(word) <= 40 or not any(char.isalnum() for char in word):
            raise UserError("Укажите слово или фразу из 2–40 символов. /filter add фраза")
        if operation == "add":
            if word in options.words:
                raise UserError("Фраза уже в списке.")
            if len(options.words) >= 40:
                raise UserError("В фильтре уже 40 фраз. Удалите одну из них перед добавлением.")
            self.sanctions.bot_can(chat, self.bot_id, DELETE)
            options.words += (word,)
        else:
            if word not in options.words:
                raise UserError("Фразы нет в списке.")
            options.words = tuple(item for item in options.words if item != word)
        self.store.save_options(chat, options)
        self.store.log(chat, "Стоп-слова", label(message["from"]), operation, word, now)
        self.reply(message, "Список стоп-слов обновлён.")

    def set_text(self, message: dict, command: str, options, args: str, now: int) -> None:
        if not args.strip():
            raise UserError(f"Добавьте текст после команды /{command}. Значение off сбрасывает настройку.")
        chat = message["chat"]["id"]
        is_rules = command == "setrules"
        if len(args) > (1600 if is_rules else 450):
            raise UserError("Текст слишком длинный. Максимум 1600 символов для правил и 450 для приветствия.")
        if is_rules:
            options.rules = args if args.casefold() != "off" else ""
        else:
            options.welcome = args.casefold() != "off"
            if options.welcome:
                options.welcome_text = args
        self.store.save_options(chat, options)
        self.store.log(chat, "Текст чата", label(message["from"]), command, "", now)
        self.reply(message, "Текст сохранён." if is_rules or options.welcome else "Приветствия выключены.")

    def private_list(self, message: dict, title: str, entries: list[str]) -> None:
        pid = message["from"]["id"]
        group = short(message["chat"].get("title", "Группа"), 70)
        title = f"{title} · {group}"
        page = title
        try:
            for entry in entries or ["Записей пока нет."]:
                combined = f"{page}\n\n{entry}"
                if len(combined.encode("utf-16-le")) // 2 > 3800:
                    self.api.send(pid, page, protected=True)
                    page = f"{title}\n\n{entry}"
                else:
                    page = combined
            self.api.send(pid, page, protected=True)
        except ApiError as exc:
            if exc.code in {400, 403}:
                self.reply(message, f"Откройте @{self.username} в личке и нажмите «Запустить», затем повторите команду.")
            else:
                self.reply(message, "Telegram не подтвердил отправку. Повторите команду чуть позже.")
        else:
            self.reply(message, "Отправлено в личку.")
