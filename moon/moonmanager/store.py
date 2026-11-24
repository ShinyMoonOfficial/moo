"""Per-group settings, counters, warnings, reports and moderation records."""

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Settings:
    rules: str = ""
    welcome_text: str = "🌙 Добро пожаловать, {name}! Правила группы — /rules."
    welcome: bool = False
    flood: bool = False
    links: bool = False
    words: tuple[str, ...] = ()
    warn_limit: int = 3
    warn_mute: int = 0


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                chat INTEGER PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                chat INTEGER NOT NULL, user INTEGER NOT NULL, name TEXT NOT NULL,
                messages INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chat, user)
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, user INTEGER NOT NULL,
                created INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS warnings_user ON warnings (chat, user, active);
            CREATE TABLE IF NOT EXISTS mutes (
                chat INTEGER NOT NULL, user INTEGER NOT NULL, expires INTEGER NOT NULL,
                permissions TEXT NOT NULL, PRIMARY KEY (chat, user)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, kind TEXT NOT NULL,
                actor TEXT NOT NULL, subject TEXT NOT NULL, note TEXT NOT NULL, created INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_chat ON audit (chat, id);
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, reporter INTEGER NOT NULL,
                subject INTEGER NOT NULL, src_message INTEGER NOT NULL, src_link TEXT NOT NULL,
                reason TEXT NOT NULL, created INTEGER NOT NULL, resolved INTEGER,
                UNIQUE (chat, reporter, src_message)
            );
            CREATE INDEX IF NOT EXISTS reports_open ON reports (chat, resolved, id);
            CREATE INDEX IF NOT EXISTS reports_sender ON reports (chat, reporter, created);
            CREATE TABLE IF NOT EXISTS reputation (
                chat INTEGER NOT NULL, sender INTEGER NOT NULL, target INTEGER NOT NULL,
                created INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reputation_sender ON reputation (chat, sender, created);
            CREATE INDEX IF NOT EXISTS reputation_target ON reputation (chat, target);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, val TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_redirects (old INTEGER PRIMARY KEY, new INTEGER NOT NULL);
        """)

    def options(self, chat: int) -> Settings:
        row = self.db.execute("SELECT payload FROM settings WHERE chat = ?", (chat,)).fetchone()
        if row is None:
            return Settings()
        fields = json.loads(row["payload"])
        fields["words"] = tuple(fields.get("words", ()))
        return Settings(**fields)

    def save_options(self, chat: int, options: Settings) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                        (chat, json.dumps(asdict(options), ensure_ascii=False)))

    def old_chat(self, chat: int) -> bool:
        return self.db.execute("SELECT 1 FROM chat_redirects WHERE old = ?", (chat,)).fetchone() is not None

    def migrate(self, old: int, new: int) -> None:
        if old == new or self.old_chat(old):
            return
        self.db.execute("INSERT INTO chat_redirects VALUES (?, ?)", (old, new))
        self.db.execute("UPDATE OR IGNORE settings SET chat = ? WHERE chat = ?", (new, old))
        self.db.execute("DELETE FROM settings WHERE chat = ?", (old,))
        self.db.execute("""
            INSERT INTO members (chat, user, name, messages)
            SELECT ?, user, name, messages FROM members WHERE chat = ?
            ON CONFLICT (chat, user) DO UPDATE SET messages = members.messages + excluded.messages
        """, (new, old))
        self.db.execute("DELETE FROM members WHERE chat = ?", (old,))
        for table in ("warnings", "audit", "reputation"):
            self.db.execute(f"UPDATE {table} SET chat = ? WHERE chat = ?", (new, old))
        for table in ("mutes", "reports"):
            self.db.execute(f"UPDATE OR IGNORE {table} SET chat = ? WHERE chat = ?", (new, old))
            self.db.execute(f"DELETE FROM {table} WHERE chat = ?", (old,))

    def remember(self, chat: int, user: int, name: str, *, count: bool = False) -> None:
        self.db.execute("""
            INSERT INTO members VALUES (?, ?, ?, ?)
            ON CONFLICT (chat, user) DO UPDATE SET
                name = excluded.name, messages = members.messages + excluded.messages
        """, (chat, user, name, int(count)))

    def name(self, chat: int, user: int) -> str:
        row = self.db.execute("SELECT name FROM members WHERE chat = ? AND user = ?", (chat, user)).fetchone()
        return row["name"] if row else f"ID {user}"

    def warn(self, chat: int, user: int, now: int) -> int:
        self.db.execute("INSERT INTO warnings (chat, user, created) VALUES (?, ?, ?)", (chat, user, now))
        return self.warning_count(chat, user)

    def unwarn(self, chat: int, user: int) -> bool:
        result = self.db.execute("""
            UPDATE warnings SET active = 0 WHERE id = (
                SELECT id FROM warnings WHERE chat = ? AND user = ? AND active = 1
                ORDER BY id DESC LIMIT 1
            )
        """, (chat, user))
        return result.rowcount > 0

    def warning_count(self, chat: int, user: int) -> int:
        return self.db.execute("""
            SELECT COUNT(*) FROM warnings WHERE chat = ? AND user = ? AND active = 1
        """, (chat, user)).fetchone()[0]

    def mark_mute(self, chat: int, user: int, expires: int, permissions: dict[str, bool]) -> None:
        self.db.execute("INSERT OR REPLACE INTO mutes VALUES (?, ?, ?, ?)",
                        (chat, user, expires, json.dumps(permissions)))

    def mute(self, chat: int, user: int) -> tuple[int, dict[str, bool]] | None:
        row = self.db.execute(
            "SELECT expires, permissions FROM mutes WHERE chat = ? AND user = ?", (chat, user)
        ).fetchone()
        return (row["expires"], json.loads(row["permissions"])) if row else None

    def clear_mute(self, chat: int, user: int) -> None:
        self.db.execute("DELETE FROM mutes WHERE chat = ? AND user = ?", (chat, user))

    def log(self, chat: int, kind: str, actor: str, subject: str, note: str, now: int) -> None:
        self.db.execute("""
            INSERT INTO audit (chat, kind, actor, subject, note, created) VALUES (?, ?, ?, ?, ?, ?)
        """, (chat, kind, actor, subject, note, now))

    def log_rows(self, chat: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM audit WHERE chat = ? ORDER BY id DESC LIMIT 12", (chat,)
        ).fetchall()

    def report(self, chat: int, reporter: int, target: int, message: int, link: str,
               reason: str, now: int) -> int | None:
        if self.db.execute("""
            SELECT 1 FROM reports WHERE chat = ? AND reporter = ? AND created > ?
        """, (chat, reporter, now - 60)).fetchone():
            return None
        result = self.db.execute("""
            INSERT OR IGNORE INTO reports (chat, reporter, subject, src_message, src_link, reason, created)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat, reporter, target, message, link, reason, now))
        return result.lastrowid if result.rowcount else None

    def reports(self, chat: int) -> list[sqlite3.Row]:
        return self.db.execute("""
            SELECT * FROM reports WHERE chat = ? AND resolved IS NULL ORDER BY id LIMIT 10
        """, (chat,)).fetchall()

    def resolve(self, chat: int, report_id: int, now: int) -> bool:
        result = self.db.execute("""
            UPDATE reports SET resolved = ? WHERE chat = ? AND id = ? AND resolved IS NULL
        """, (now, chat, report_id))
        return result.rowcount > 0

    def commend(self, chat: int, sender: int, target: int, now: int) -> bool:
        rows = self.db.execute("""
            SELECT target FROM reputation WHERE chat = ? AND sender = ? AND created > ?
        """, (chat, sender, now - 86400)).fetchall()
        if len(rows) >= 5 or any(row["target"] == target for row in rows):
            return False
        self.db.execute("INSERT INTO reputation VALUES (?, ?, ?, ?)", (chat, sender, target, now))
        return True

    def profile(self, chat: int, user: int) -> tuple[int, int]:
        member = self.db.execute("SELECT messages FROM members WHERE chat = ? AND user = ?",
                                 (chat, user)).fetchone()
        score = self.db.execute("SELECT COUNT(*) FROM reputation WHERE chat = ? AND target = ?",
                                (chat, user)).fetchone()[0]
        return (member["messages"] if member else 0, score)

    def leaderboard(self, chat: int, by_reputation: bool) -> list[sqlite3.Row]:
        if by_reputation:
            return self.db.execute("""
                SELECT members.name, scores.score FROM (
                    SELECT target, COUNT(*) AS score FROM reputation WHERE chat = ? GROUP BY target
                ) AS scores JOIN members ON members.chat = ? AND members.user = scores.target
                ORDER BY score DESC, scores.target LIMIT 10
            """, (chat, chat)).fetchall()
        return self.db.execute("""
            SELECT name, messages AS score FROM members WHERE chat = ? AND messages > 0
            ORDER BY score DESC, user LIMIT 10
        """, (chat,)).fetchall()

    def offset(self) -> int | None:
        row = self.db.execute("SELECT val FROM meta WHERE key = 'update_offset'").fetchone()
        if row:
            offset, saved_at = json.loads(row["val"])
            if time.time() - saved_at < 6 * 86400:
                return offset
        return None

    def set_offset(self, offset: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('update_offset', ?)",
                        (json.dumps([offset, time.time()]),))
