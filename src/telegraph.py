"""Publish text (e.g. a mediainfo report) to telegra.ph and return the public page URL(s).

Telegraph pages are public-by-URL (unlisted) - the same privacy model as the nginx download
links this bot already hands out. We create one authorship account lazily and cache its access
token on the state volume, so every page shares an author across restarts. Telegraph caps a
page's content at 64 KB, so a long report (a whole season of files) is split across several
pages, each file rendered as a monospace <pre> block under an <h4> heading.
"""
import asyncio
import json
import os

import aiohttp

from . import config

API = "https://api.telegra.ph"
_TOKEN_FILE = config.STATE_DIR / "telegraph_token.txt"
_MAX_CONTENT = 60_000            # Telegraph hard-caps page content at 64 KB; stay safely under
_TIMEOUT = aiohttp.ClientTimeout(total=30)

_lock = asyncio.Lock()
_token_cache: str | None = None


async def _token(session: aiohttp.ClientSession) -> str:
    """The Telegraph access token, created once and cached (env -> file -> createAccount).
    Guarded by a lock so two concurrent jobs never race to create two accounts."""
    global _token_cache
    if _token_cache:
        return _token_cache
    env = os.environ.get("TELEGRAPH_TOKEN", "").strip()
    if env:
        _token_cache = env
        return env
    async with _lock:
        if _token_cache:                                    # another job won the race while we waited
            return _token_cache
        try:
            if _TOKEN_FILE.exists():
                tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
                if tok:
                    _token_cache = tok
                    return tok
        except OSError:
            pass
        async with session.get(f"{API}/createAccount",
                               params={"short_name": "unshackle", "author_name": "unshackle"}) as r:
            data = await r.json()
        tok = (data.get("result") or {}).get("access_token") or ""
        if not tok:
            raise RuntimeError(f"telegraph createAccount failed: {data}")
        try:
            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_FILE.write_text(tok, encoding="utf-8")
        except OSError:
            pass
        _token_cache = tok
        return tok


def _section_nodes(heading: str, body: str) -> list:
    """One report section as Telegraph DOM nodes: an <h4> heading + a monospace <pre> block."""
    nodes = []
    if heading:
        nodes.append({"tag": "h4", "children": [heading]})
    nodes.append({"tag": "pre", "children": [body]})
    return nodes


def _node_bytes(nodes: list) -> int:
    return len(json.dumps(nodes, ensure_ascii=False).encode("utf-8"))


async def _create_page(session: aiohttp.ClientSession, token: str, title: str, nodes: list) -> str:
    async with session.post(f"{API}/createPage", data={
            "access_token": token,
            "title": (title or "MediaInfo")[:256],
            "content": json.dumps(nodes, ensure_ascii=False)}) as r:
        data = await r.json()
    url = (data.get("result") or {}).get("url") or ""
    if not url:
        raise RuntimeError(f"telegraph createPage failed: {data}")
    return url


def _paginate(sections: list[tuple[str, str]]) -> list[list]:
    """Group report sections into pages of Telegraph DOM nodes, each page's serialized content
    kept under the per-page cap. A single section too large on its own has its body truncated."""
    pages: list[list] = []
    cur: list = []
    cur_size = 0
    for heading, body in sections:
        nodes = _section_nodes(heading, body)
        size = _node_bytes(nodes)
        if size > _MAX_CONTENT:                             # a single oversize section: truncate its body
            body = body[:_MAX_CONTENT // 2] + "\n... (truncated)"
            nodes = _section_nodes(heading, body)
            size = _node_bytes(nodes)
        if cur and cur_size + size > _MAX_CONTENT:          # would overflow this page -> start a new one
            pages.append(cur)
            cur, cur_size = [], 0
        cur.extend(nodes)
        cur_size += size
    if cur:
        pages.append(cur)
    return pages


async def publish(title: str, sections: list[tuple[str, str]]) -> list[str]:
    """Publish [(heading, body_text), ...] to telegra.ph and return the page URL(s). Splits into
    several pages when the content would exceed Telegraph's per-page cap. Raises on total failure."""
    pages = _paginate(sections)
    if not pages:
        return []
    urls = []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        token = await _token(session)
        for i, nodes in enumerate(pages):
            page_title = title if len(pages) == 1 else f"{title} ({i + 1}/{len(pages)})"
            urls.append(await _create_page(session, token, page_title, nodes))
    return urls
