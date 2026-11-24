"""Automatic filters for messages and captions."""

import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field

LINK = re.compile(r"(?<![\w@])(?:https?://|tg://|www\.|(?:t|telegram)\.me/)", re.IGNORECASE)
FLOOD_SECONDS = 8
FLOOD_LIMIT = 7
DUPLICATE_SECONDS = 25
DUPLICATE_LIMIT = 3


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def is_link(text: str, entities: list[dict]) -> bool:
    return any(entity["type"] in {"url", "text_link"} for entity in entities) or bool(LINK.search(text))


def contains_word(text: str, words: tuple[str, ...]) -> bool:
    value = normalized(text)
    return any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", value) for word in words)


@dataclass
class Window:
    posts: deque = field(default_factory=deque)
    last_seen: int = 0
    notice_at: int = 0


class Flood:
    def __init__(self):
        self.windows: dict[tuple[int, int], Window] = {}
        self.cleanup_at = 0

    def window(self, chat: int, user: int, now: int) -> Window:
        if now >= self.cleanup_at:
            self.windows = {
                key: value for key, value in self.windows.items() if value.last_seen >= now - 60
            }
            self.cleanup_at = now + 60
        state = self.windows.setdefault((chat, user), Window())
        state.last_seen = now
        return state

    def too_many(self, chat: int, user: int, now: int, message: dict, text: str) -> bool:
        state = self.window(chat, user, now)
        when = min(message.get("date", now), now)
        group = message.get("media_group_id")
        if state.posts:
            when = max(when, state.posts[-1][0])
        while state.posts and state.posts[0][0] <= when - DUPLICATE_SECONDS:
            state.posts.popleft()
        if group and any(post[1] == group for post in state.posts):
            return False
        plain = normalized(text)[:512] if len(text) >= 12 else ""
        state.posts.append((when, group, plain))
        recent = sum(stamp > when - FLOOD_SECONDS for stamp, _, _ in state.posts)
        repeats = sum(plain == value for _, _, value in state.posts) if plain else 0
        # Bound the window for an account sending large bursts.
        if len(state.posts) > 30:
            state.posts.popleft()
        return recent >= FLOOD_LIMIT or repeats >= DUPLICATE_LIMIT

    def should_notify(self, chat: int, user: int, now: int) -> bool:
        state = self.window(chat, user, now)
        if state.notice_at > now:
            return False
        state.notice_at = now + 30
        return True
