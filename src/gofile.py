"""
B.H. Copyright (c) 2026 Yemot HaMashiach Ltd.
All Rights Reserved.

This software is the confidential and proprietary information of
Yemot HaMashiach Ltd. ("Confidential Information"). You shall not
disclose such Confidential Information and shall use it only in
accordance with the terms of the license agreement you entered into
with Yemot HaMashiach Ltd.

Unauthorized copying of this file, via any medium, is strictly prohibited.
"""
"""
gofile.io delivery: upload local files and get a public, no-size-limit download link.

Used as the delivery channel for files over Telegram's cap (e.g. 4K recordings), and -
when the user opts in - as an extra "download link" alongside the Telegram file for any
size. gofile takes the bandwidth and storage, so we never have to hold large files on our
own server.

A whole collection (a season, a podcast, an album - every file of one download job) goes
into ONE gofile folder and yields ONE link, not a link per file. The first upload creates
the folder; the rest reuse its folderId; the folder's download page lists them all. A single
file is just a folder of one. Use a `Session` to group:

    async with gofile.Session() as gf:
        for f in files:
            await gf.add(f, progress=cb)
        link = gf.link            # one https://gofile.io/d/<code> for the whole job

Verified API (June 2026, gofile v3.x):
  POST https://api.gofile.io/accounts                    -> {data:{token}}   (guest, no signup)
  GET  https://api.gofile.io/servers                     -> {data:{servers:[{name,zone}]}}
  POST https://{server}.gofile.io/contents/uploadfile    (multipart 'file' [+ 'folderId'],
       Bearer token) -> {data:{downloadPage, parentFolder, parentFolderCode, id, ...}}

Download (listing a folder) is intentionally NOT implemented here: gofile gates the contents
API behind a per-request anti-bot token (generateWT) computed by obfuscated, environment-
dependent browser JS, so a server-side reproduction is brittle. Pulling FROM gofile is left
to the yt-dlp catch-all (whose maintainers track that token), or a headless browser if needed.
"""
import asyncio
import os
import time

import aiohttp

API = "https://api.gofile.io"
# avi1 sits in the EU (Hetzner DE); prefer an EU store so the upload hop is short.
PREFERRED_ZONE = os.environ.get("GOFILE_ZONE", "eu")
# Optional account token: set GOFILE_TOKEN to upload into a persistent account (so links are
# manageable / longer-lived). Empty -> a throwaway guest token (links still public, may expire).
ACCOUNT_TOKEN = os.environ.get("GOFILE_TOKEN", "").strip()

_TOKEN_TTL = 3600                       # refresh a guest token at most hourly
_token: str = ""
_token_at: float = 0.0
_lock = asyncio.Lock()


async def _get_token(session: aiohttp.ClientSession) -> str:
    """A bearer token for uploads: a configured account token, else a cached guest token."""
    global _token, _token_at
    if ACCOUNT_TOKEN:
        return ACCOUNT_TOKEN
    async with _lock:
        if _token and (time.time() - _token_at) < _TOKEN_TTL:
            return _token
        async with session.post(f"{API}/accounts", json={}) as r:
            doc = await r.json(content_type=None)
        tok = ((doc or {}).get("data") or {}).get("token") or ""
        if not tok:
            raise RuntimeError(f"gofile: could not get an upload token ({doc})")
        _token, _token_at = tok, time.time()
        return _token


async def _pick_server(session: aiohttp.ClientSession) -> str:
    """An available upload server, preferring the nearest zone."""
    async with session.get(f"{API}/servers") as r:
        doc = await r.json(content_type=None)
    servers = ((doc or {}).get("data") or {}).get("servers") or []
    if not servers:
        raise RuntimeError(f"gofile: no upload servers available ({doc})")
    for s in servers:
        if (s.get("zone") or "") == PREFERRED_ZONE:
            return s["name"]
    return servers[0]["name"]


class Session:
    """Groups one download's files into a single gofile folder -> one shared link.

    Lazily gets a token + server on the first add(); the first file's parentFolder becomes the
    folder every later file is uploaded into. `link` is the folder's public download page (None
    until the first successful add). Each add() raises on failure so the caller can fall back.
    """

    def __init__(self, timeout: int = 7200):
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._token = ""
        self._server = ""
        self._folder_id: str | None = None
        self.link: str | None = None

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _ensure(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        if not self._token:
            self._token = await _get_token(self._session)
        if not self._server:
            self._server = await _pick_server(self._session)

    async def add(self, path: str, progress=None) -> str:
        """Upload one file into this session's folder. Returns the folder link."""
        if not (path and os.path.exists(path) and os.path.getsize(path) > 0):
            raise RuntimeError("gofile: nothing to upload (missing/empty file)")
        await self._ensure()
        total = os.path.getsize(path)
        fh = open(path, "rb")
        watcher = None
        try:
            form = aiohttp.FormData()
            form.add_field("file", fh, filename=os.path.basename(path),
                           content_type="application/octet-stream")
            if self._folder_id:                      # land in the same folder as earlier files
                form.add_field("folderId", self._folder_id)

            if progress and total:
                # aiohttp streams by advancing the file handle; poll its position for bytes-sent.
                async def _watch():
                    last = -1
                    while True:
                        await asyncio.sleep(2)
                        try:
                            pos = fh.tell()
                        except Exception:
                            return
                        if pos != last and 0 < pos <= total:
                            last = pos
                            try:
                                await progress(pos, total)
                            except Exception:
                                pass
                watcher = asyncio.ensure_future(_watch())

            url = f"https://{self._server}.gofile.io/contents/uploadfile"
            async with self._session.post(url, data=form,
                                          headers={"Authorization": f"Bearer {self._token}"}) as r:
                doc = await r.json(content_type=None)
        finally:
            if watcher:
                watcher.cancel()
            try:
                fh.close()
            except Exception:
                pass

        if not isinstance(doc, dict) or doc.get("status") != "ok":
            raise RuntimeError(f"gofile: upload failed ({(doc or {}).get('status', doc)})")
        data = doc.get("data") or {}
        page = data.get("downloadPage")
        if not page:
            raise RuntimeError(f"gofile: no downloadPage in response ({doc})")
        if self._folder_id is None:                  # first file: adopt its folder for the rest
            self._folder_id = data.get("parentFolder")
        self.link = page
        return page


async def upload(path: str, progress=None, timeout: int = 7200) -> str:
    """Convenience: upload a single file and return its public download-page URL."""
    async with Session(timeout=timeout) as gf:
        return await gf.add(path, progress=progress)
