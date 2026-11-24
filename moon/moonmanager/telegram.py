"""Long-polling Telegram transport using only the Python standard library."""

import http.client
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, code: int, parameters: dict | None = None):
        super().__init__(f"Telegram API ({code})")
        self.code = code
        self.parameters = parameters or {}


class Telegram:
    def __init__(self, token: str):
        self.url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, *, http_timeout: int = 10, **params):
        request = Request(
            f"{self.url}/{method}", json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=http_timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            try:
                with exc:
                    payload = json.load(exc)
            except (ValueError, OSError, http.client.HTTPException):
                raise ApiError(exc.code) from None
            raise ApiError(payload.get("error_code", exc.code), payload.get("parameters")) from None
        except (URLError, OSError, ValueError, http.client.HTTPException):
            raise ApiError(0) from None
        if not payload.get("ok"):
            raise ApiError(payload.get("error_code", 0), payload.get("parameters"))
        return payload["result"]

    def member(self, chat: int, user: int) -> dict:
        return self.call("getChatMember", chat_id=chat, user_id=user)

    def send(self, chat: int, text: str, *, thread: int | None = None,
             protected: bool = False) -> None:
        params = {"message_thread_id": thread} if thread is not None else {}
        self.call("sendMessage", chat_id=chat, text=text, protect_content=protected,
                  link_preview_options={"is_disabled": True}, **params)


def poll(api: Telegram, bot) -> None:
    delay = 1
    choices = ["message", "edited_message"]
    while True:
        request = {"timeout": 25, "allowed_updates": choices}
        offset = bot.store.offset()
        if offset is not None:
            request["offset"] = offset
        try:
            # getUpdates' server-side timeout is separate from the HTTP timeout.
            updates = api.call("getUpdates", http_timeout=38, **request)
        except ApiError as exc:
            if exc.code in {401, 403, 404, 409}:
                raise
            wait = min(60, exc.parameters.get("retry_after", delay))
            LOG.warning("Telegram не ответил на опрос (код %s). Повтор через %s с.", exc.code, wait)
            time.sleep(wait)
            delay = min(30, delay * 2)
            continue
        delay = 1
        for update in updates:
            with bot.store.db:
                bot.handle(update, int(time.time()))
                bot.store.set_offset(update["update_id"] + 1)
