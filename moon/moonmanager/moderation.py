"""Permission checks and sanctions against individual group members."""

import re

from .telegram import ApiError

ADMIN = {"creator", "administrator"}
PRESENT = {"member", "restricted"}
DELETE = "can_delete_messages"
RESTRICT = "can_restrict_members"
SETTINGS = "can_change_info"
SEND_PERMISSIONS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
    "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
)
OTHER_PERMISSIONS = (
    "can_react_to_messages", "can_edit_tag", "can_change_info", "can_invite_users",
    "can_pin_messages", "can_manage_topics",
)
PERMISSION_DEFAULTS = {
    "can_react_to_messages": "can_send_messages",
    "can_edit_tag": "can_pin_messages",
    "can_manage_topics": "can_pin_messages",
}
RIGHT_NAMES = {
    DELETE: "Удаление сообщений", RESTRICT: "Блокировка пользователей",
    SETTINGS: "Изменение профиля группы",
}
DURATION = re.compile(r"([1-9][0-9]{0,4})([mhdмчд])", re.IGNORECASE)
FACTORS = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}


class UserError(ValueError):
    """A safe message about a rejected action."""


def parse_duration(value: str) -> int:
    match = DURATION.fullmatch(value)
    if match:
        count, unit = match.groups()
        seconds = int(count) * FACTORS[unit.lower()]
        if seconds <= 30 * 86400:
            return seconds
    raise UserError("Укажите срок от 1 минуты до 30 дней. Примеры: 10m, 2h, 3d.")


def human_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} дн."
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч"
    return f"{seconds // 60} мин"


def is_admin(member: dict) -> bool:
    return member.get("status") in ADMIN


def need_right(member: dict, right: str | None = None) -> None:
    if not is_admin(member):
        raise UserError("Команда доступна администраторам группы.")
    if right and member["status"] != "creator" and not member.get(right):
        raise UserError(f"Для этой команды нужно право «{RIGHT_NAMES[right]}».")


def present(member: dict) -> bool:
    status = member.get("status")
    return status in PRESENT and (status != "restricted" or member.get("is_member", False))


class Sanctions:
    def __init__(self, api, store):
        self.api = api
        self.store = store

    def bot_can(self, chat: int, bot: int, right: str) -> None:
        member = self.api.member(chat, bot)
        if not is_admin(member) or not member.get(right):
            raise UserError(f"Выдайте MoonManager право «{RIGHT_NAMES[right]}» в Telegram.")

    def own_mute(self, chat: int, user: int, target: dict, now: int) -> bool:
        stored = self.store.mute(chat, user)
        if not stored or target.get("status") != "restricted":
            return False
        expires, permissions = stored
        return (
            expires > now and target.get("until_date") == expires
            and all(bool(target.get(field)) == allowed for field, allowed in permissions.items())
        )

    def mute(self, chat: int, user: int, target: dict, now: int, duration: int) -> None:
        if not present(target):
            raise UserError("Этот участник сейчас вне группы.")
        if target["status"] == "restricted" and not self.own_mute(chat, user, target, now):
            raise UserError("На участника уже действуют ограничения. Измените их в Telegram.")
        group = self.api.call("getChat", chat_id=chat)
        allowed = group.get("permissions") or {}
        permissions = {
            field: bool(allowed.get(field, allowed.get(PERMISSION_DEFAULTS.get(field))))
            for field in OTHER_PERMISSIONS
        }
        permissions.update({field: False for field in SEND_PERMISSIONS})
        expires = now + duration
        self.api.call("restrictChatMember", chat_id=chat, user_id=user,
                      permissions=permissions, use_independent_chat_permissions=True,
                      until_date=expires)
        self.store.mark_mute(chat, user, expires, permissions)

    def unmute(self, chat: int, user: int, target: dict, now: int) -> None:
        if not self.own_mute(chat, user, target, now):
            raise UserError("У участника нет действующего мута, выданного MoonManager.")
        permissions = {field: True for field in (*SEND_PERMISSIONS, *OTHER_PERMISSIONS)}
        self.api.call("restrictChatMember", chat_id=chat, user_id=user,
                      permissions=permissions, use_independent_chat_permissions=True)
        self.store.clear_mute(chat, user)

    def ban(self, chat: int, user: int, expires: int | None) -> None:
        params = {"until_date": expires} if expires is not None else {}
        self.api.call("banChatMember", chat_id=chat, user_id=user, revoke_messages=False, **params)
        self.store.clear_mute(chat, user)

    def unban(self, chat: int, user: int) -> None:
        self.api.call("unbanChatMember", chat_id=chat, user_id=user, only_if_banned=True)

    def kick(self, chat: int, user: int, now: int) -> bool:
        # A brief ban makes the operation recoverable if Telegram rejects the follow-up.
        self.ban(chat, user, now + 75)
        try:
            self.unban(chat, user)
        except ApiError:
            return False
        return True
