"""Telegram Bot API transport: a thin async wrapper over the raw HTTP API plus the
inline-keyboard helpers. Depends only on aiohttp and the bot token, so it stays a leaf."""
import json

import aiohttp

from . import config

API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}"

PAGE = 8        # items per list page (one button per row)
GRID_N = 12     # items per grid page (4 per row x 3 rows)


async def call(method: str, **params):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API}/{method}", json=params) as r:
            return await r.json()


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def grid_rows(pairs: list[tuple[str, str]], per_row: int = 4) -> list[list[tuple[str, str]]]:
    """Lay out short-label (text, callback) buttons several-per-row. Reused everywhere."""
    return [pairs[i:i + per_row] for i in range(0, len(pairs), per_row)]


async def send(chat: int, text: str, rows=None):
    return await call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                      reply_markup=kb(rows or []), disable_web_page_preview=True)


async def edit(chat: int, mid: int, text: str, rows=None):
    return await call("editMessageText", chat_id=chat, message_id=mid, text=text,
                      parse_mode="HTML", reply_markup=kb(rows or []), disable_web_page_preview=True)


async def send_document(chat: int, filename: str, content: bytes, caption: str = "", rows=None):
    """Send an in-memory file as a document (multipart). Used for small generated files like the
    keys JSON - no temp file on disk needed."""
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat))
    if caption:
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
    if rows:
        form.add_field("reply_markup", json.dumps(kb(rows)))
    form.add_field("document", content, filename=filename, content_type="application/octet-stream")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API}/sendDocument", data=form) as r:
            return await r.json()
