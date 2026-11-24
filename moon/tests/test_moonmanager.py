import json
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from moonmanager.bot import Bot
from moonmanager.filters import Flood, contains_word
from moonmanager.moderation import DELETE, RESTRICT, SETTINGS, UserError, parse_duration
from moonmanager.store import Store
from moonmanager.telegram import ApiError, Telegram, poll

CHAT = -1001234567890
OTHER_CHAT = CHAT - 1
OWNER = 1
MEMBER = 2
MOD = 3
PEER = 4
BOT = 101
NOW = 1_785_000_000
GROUP = {"id": CHAT, "title": "MoonChat", "type": "supergroup"}


def user(pid, *, bot=False):
    return {"id": pid, "first_name": f"Участник {pid}", "is_bot": bot}


def member(pid, status="member", **rights):
    return {"user": user(pid, bot=(pid == BOT)), "status": status, **rights}


class FakeTelegram:
    def __init__(self):
        self.members = {}
        for chat in (CHAT, OTHER_CHAT):
            self.members[chat] = {
                OWNER: member(OWNER, "creator"),
                MEMBER: member(MEMBER),
                MOD: member(MOD, "administrator", **{DELETE: True}),
                PEER: member(PEER),
                BOT: member(BOT, "administrator", **{DELETE: True, RESTRICT: True}),
            }
        self.calls = []
        self.messages = []
        self.fail = {}

    def member(self, chat, pid):
        self.calls.append(("getChatMember", {"chat_id": chat, "user_id": pid}))
        return self.members[chat][pid]

    def send(self, chat, text, *, thread=None, protected=False):
        self.messages.append((chat, text, thread, protected))

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == "getChat":
            return {"permissions": {"can_send_messages": True, "can_invite_users": True}}
        if method == "restrictChatMember":
            chat, pid = params["chat_id"], params["user_id"]
            if params.get("until_date"):
                self.members[chat][pid] = member(
                    pid, "restricted", is_member=True, until_date=params["until_date"],
                    **params["permissions"],
                )
            else:
                self.members[chat][pid] = member(pid)
        if method == "banChatMember":
            self.members[params["chat_id"]][params["user_id"]] = member(
                params["user_id"], "kicked", until_date=params.get("until_date", 0),
            )
        if method == "unbanChatMember":
            self.members[params["chat_id"]][params["user_id"]] = member(params["user_id"], "left")
        return True

    def actions(self, method):
        return [params for name, params in self.calls if name == method]


class BotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.db.close())
        self.api = FakeTelegram()
        self.bot = Bot(self.api, self.store, {"id": BOT, "username": "moonmanager_test_bot"})
        self.msg_id = 20

    def message(self, pid, text="", *, target=None, chat=GROUP, now=NOW, edit=False,
                options=None, original_id=10):
        self.msg_id += 1
        message = {"chat": chat, "from": user(pid), "date": now,
                   "message_id": self.msg_id, "text": text, **(options or {})}
        if target is not None:
            message["reply_to_message"] = {"from": user(target), "message_id": original_id}
        with self.store.db:
            self.bot.handle({"edited_message" if edit else "message": message}, now)
        return self.msg_id

    def text(self):
        return self.api.messages[-1][1]

    def test_settings_are_scoped_and_change_info_checked(self):
        self.message(MEMBER, "/setrules Тайком")
        self.assertIn("доступна администраторам", self.text())
        self.message(MOD, "/setrules От модератора")
        self.assertIn("Изменение профиля группы", self.text())
        self.message(OWNER, "/setrules Без флуда\nУважайте участников")
        self.message(PEER, "/rules")
        self.assertIn("Без флуда\nУважайте участников", self.text())
        self.message(PEER, "/rules", chat={**GROUP, "id": OTHER_CHAT})
        self.assertIn("не заданы", self.text())
        self.message(OWNER, "/check")
        self.assertIn("Блокировка пользователей · да", self.text())

    def test_warning_and_permission_checks(self):
        self.message(MEMBER, "/warn причина", target=PEER)
        self.message(MOD, "/warn причина", target=PEER)
        self.assertEqual(self.store.warning_count(CHAT, PEER), 0)
        self.message(OWNER, "/warn причина", target=MOD)
        self.assertEqual(self.store.warning_count(CHAT, MOD), 0)
        self.message(OWNER, "/warn Пример", target=PEER)
        self.assertEqual(self.store.warning_count(CHAT, PEER), 1)
        self.message(OWNER, f"/unwarn {PEER}")
        self.assertEqual(self.store.warning_count(CHAT, PEER), 0)
        self.assertEqual(len(self.api.actions("restrictChatMember")), 0)

    def test_anonymous_admin_cannot_dispatch(self):
        self.message(OWNER, "/ban forever", target=MEMBER, options={"sender_chat": GROUP})
        self.assertIn("личного профиля", self.text())
        self.assertFalse(self.api.actions("banChatMember"))

    def test_automatic_mute_on_threshold_and_restart(self):
        self.message(OWNER, "/config warnlimit 2")
        self.message(OWNER, "/config warnmute 3m")
        self.message(OWNER, "/warn Причина", target=MEMBER)
        self.assertFalse(self.api.actions("restrictChatMember"))
        self.message(OWNER, "/warn Вторая причина", target=MEMBER)
        mute = self.api.actions("restrictChatMember")[-1]
        self.assertEqual(mute["until_date"], NOW + 180)
        self.assertFalse(mute["permissions"]["can_send_messages"])
        self.assertTrue(mute["permissions"]["can_invite_users"])
        self.store.db.close()
        self.store = Store(self.path)
        self.bot = Bot(self.api, self.store, {"id": BOT, "username": "moonmanager_test_bot"})
        self.assertEqual(self.store.warning_count(CHAT, MEMBER), 2)
        self.message(OWNER, "/unmute", target=MEMBER, now=NOW + 1)
        lift = self.api.actions("restrictChatMember")[-1]
        self.assertTrue(all(lift["permissions"].values()))
        self.assertIsNone(self.store.mute(CHAT, MEMBER))

    def test_manual_and_changed_mutes_are_protected(self):
        self.api.members[CHAT][MEMBER] = member(MEMBER, "restricted", is_member=True, until_date=NOW + 1234)
        self.message(OWNER, "/mute 5m", target=MEMBER)
        self.message(OWNER, "/unmute", target=MEMBER)
        self.assertFalse(self.api.actions("restrictChatMember"))
        self.api.members[CHAT][MEMBER] = member(MEMBER)
        self.message(OWNER, "/mute 5m", target=MEMBER)
        self.api.members[CHAT][MEMBER]["can_invite_users"] = False
        self.message(OWNER, "/unmute", target=MEMBER)
        self.assertEqual(len(self.api.actions("restrictChatMember")), 1)

    def test_explicit_duration_for_bans_unban_and_recoverable_kick(self):
        self.message(OWNER, "/ban без срока", target=MEMBER)
        self.assertFalse(self.api.actions("banChatMember"))
        self.message(OWNER, "/ban 2h Причина", target=MEMBER)
        self.assertEqual(NOW + 7200, self.api.actions("banChatMember")[-1]["until_date"])
        self.message(OWNER, f"/unban {MEMBER}")
        self.assertTrue(self.api.actions("unbanChatMember")[-1]["only_if_banned"])
        self.message(OWNER, f"/unban {MEMBER}")
        self.assertEqual(len(self.api.actions("unbanChatMember")), 1)
        self.api.fail["unbanChatMember"] = ApiError(429, {"retry_after": 30})
        self.message(OWNER, "/kick", target=PEER)
        self.assertEqual(NOW + 75, self.api.actions("banChatMember")[-1]["until_date"])
        self.assertIn("истечёт", self.text())

    def test_links_and_edits_only_after_opt_in_with_admin_exemption(self):
        bad = {"caption": "фото", "caption_entities": [{"type": "text_link", "url": "https://example.org"}]}
        self.message(MEMBER, options=bad)
        self.assertFalse(self.api.actions("deleteMessage"))
        self.message(MOD, "/config links on")
        expected = self.message(MEMBER, options=bad, edit=True)
        self.message(MOD, "https://example.org")
        self.assertEqual([expected], [action["message_id"] for action in self.api.actions("deleteMessage")])
        self.assertEqual(self.store.profile(CHAT, MEMBER)[0], 1)

    def test_missing_delete_rights_and_filter_rules(self):
        self.api.members[CHAT][BOT][DELETE] = False
        self.message(OWNER, "/config links on")
        self.assertFalse(self.store.options(CHAT).links)
        self.api.members[CHAT][BOT][DELETE] = True
        self.message(OWNER, "/filter add стоп слово")
        expected = self.message(MEMBER, "СТОП    СЛОВО!")
        self.message(PEER, "стоп словообразование")
        self.assertEqual([expected], [action["message_id"] for action in self.api.actions("deleteMessage")])

    def test_welcome_thread_and_placeholders(self):
        self.message(OWNER, "/setwelcome Привет, {name}! Ты в {chat}.")
        self.message(
            OWNER, options={"new_chat_members": [user(MEMBER), user(BOT, bot=True)],
                            "is_topic_message": True, "message_thread_id": 42},
        )
        self.assertEqual(self.api.messages[-1][:3], (CHAT, f"Привет, Участник {MEMBER}! Ты в MoonChat.", 42))

    def test_reputation_from_reply_with_limits_and_separate_groups(self):
        self.message(MEMBER, "Обычное сообщение")
        self.message(MEMBER, "/rep", target=MEMBER)
        self.message(PEER, "+реп", target=MEMBER)
        self.message(PEER, "/rep", target=MEMBER, now=NOW + 10)
        self.assertEqual((1, 1), self.store.profile(CHAT, MEMBER))
        self.assertEqual((0, 0), self.store.profile(OTHER_CHAT, MEMBER))
        self.message(OWNER, "/top rep")
        self.assertIn("Участник 2 · 1", self.text())

    def test_reports_go_only_to_requesting_admin_and_close_in_same_group(self):
        self.message(MEMBER, "/report Причина для админа", target=PEER)
        self.message(MEMBER, "/reports")
        self.assertIn("доступна администраторам", self.text())
        self.message(OWNER, "/reports")
        private = [entry for entry in self.api.messages if "Причина для админа" in entry[1]]
        self.assertEqual([(row[0], row[3]) for row in private], [(OWNER, True)])
        self.assertIn("https://t.me/c/1234567890/10", private[0][1])
        number = self.store.reports(CHAT)[0]["id"]
        self.message(OWNER, f"/resolve {number}", chat={**GROUP, "id": OTHER_CHAT})
        self.assertEqual(len(self.store.reports(CHAT)), 1)
        self.message(OWNER, f"/resolve {number}")
        self.assertFalse(self.store.reports(CHAT))

    def test_nuke_commands_ignored_when_edited_or_meant_for_other_bot(self):
        self.message(OWNER, "/ban@Another_bot forever", target=MEMBER)
        self.message(OWNER, "/ban forever", target=MEMBER, edit=True)
        self.assertFalse(self.api.actions("banChatMember"))

    def test_migration_transfers_rules_counter_and_warnings(self):
        self.message(OWNER, "/setrules Действующие правила")
        self.message(MEMBER, "Первое")
        self.message(OWNER, "/warn Причина", target=MEMBER)
        self.message(OWNER, options={"migrate_to_chat_id": OTHER_CHAT})
        self.assertEqual(self.store.options(OTHER_CHAT).rules, "Действующие правила")
        self.assertEqual(self.store.profile(OTHER_CHAT, MEMBER), (1, 0))
        self.assertEqual(self.store.warning_count(OTHER_CHAT, MEMBER), 1)


class FilterTests(unittest.TestCase):
    def test_flood_counts_album_once_and_detects_regular_burst(self):
        flood = Flood()
        for _ in range(10):
            self.assertFalse(flood.too_many(CHAT, MEMBER, NOW, {"media_group_id": "x"}, ""))
        for _ in range(5):
            self.assertFalse(flood.too_many(CHAT, MEMBER, NOW, {}, ""))
        self.assertTrue(flood.too_many(CHAT, MEMBER, NOW, {}, ""))

    def test_long_duplicate_and_unicode_boundaries(self):
        flood = Flood()
        for n in range(3):
            self.assertEqual(n == 2, flood.too_many(CHAT, MEMBER, NOW + n * 5, {}, "Повтор сообщения"))
        self.assertTrue(contains_word("Всё! СТОП СЛОВО!", ("стоп слово",)))
        self.assertFalse(contains_word("Стоп словообразование", ("стоп слово",)))

    def test_duration_bounds(self):
        self.assertEqual(parse_duration("1ч"), 3600)
        self.assertEqual(parse_duration("30d"), 2592000)
        with self.assertRaises(UserError):
            parse_duration("31d")
        with self.assertRaises(UserError):
            parse_duration("0m")


class TransportTests(unittest.TestCase):
    def test_poll_uses_server_timeout_and_commits_offset(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        api = Telegram("0:TestFixtureTokenIsNotReal")
        api_bot = Bot(api, store, {"id": BOT, "username": "moonmanager_test_bot"})
        seen = []
        update = {"update_id": 120, "message": {
            "chat": GROUP, "message_id": 50, "from": user(MEMBER), "date": int(time.time()), "text": "Сообщение",
        }}

        def open_mock(request, timeout):
            params = json.loads(request.data)
            seen.append((params, timeout))
            payload = {"ok": True, "result": [update]} if len(seen) == 1 else {"ok": False, "error_code": 409}
            return BytesIO(json.dumps(payload).encode())

        with patch("moonmanager.telegram.urlopen", side_effect=open_mock), self.assertRaises(ApiError):
            poll(api, api_bot)
        self.assertEqual(seen[0], ({"timeout": 25, "allowed_updates": ["message", "edited_message"]}, 38))
        self.assertEqual(seen[1][0]["offset"], 121)
        self.assertEqual(store.profile(CHAT, MEMBER), (1, 0))


if __name__ == "__main__":
    unittest.main()
