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


# --------------------------------------------------------------------------
# Download FROM gofile
# --------------------------------------------------------------------------
# gofile gates its contents API behind a per-request website-token (generateWT) computed by
# obfuscated JS that fingerprints the JS runtime - node/deno/browser each produce a different
# value and only a real browser's is accepted. So we resolve the folder in a real (headless)
# browser via Playwright - the same proven pattern the MAKO service uses - which runs that JS
# natively and yields a valid listing. Each file's direct CDN link then downloads over HTTP with
# the browser's accountToken cookie. This is robust to gofile rotating the obfuscation.

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")
_VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg")


def parse_code(url: str) -> str:
    """The folder code from a gofile URL or a bare code. gofile.io/d/<code> -> <code>."""
    import re
    m = re.search(r"gofile\.io/(?:d|w)/([A-Za-z0-9]+)", url or "")
    if m:
        return m.group(1)
    s = (url or "").strip().strip("/")
    return s if re.fullmatch(r"[A-Za-z0-9]+", s) else ""


def _is_video(name: str, mimetype: str) -> bool:
    mt = (mimetype or "").lower()
    if mt.startswith("video/"):
        return True
    return any((name or "").lower().endswith(e) for e in _VIDEO_EXT)


async def resolve(url: str, timeout_ms: int = 45000) -> dict:
    """Resolve a gofile folder in a headless browser. Returns
    {folder, token, files:[{name,link,size,mimetype,type,is_video}], subfolders}.
    Raises if Playwright is unavailable or nothing could be captured."""
    code = parse_code(url)
    if not code:
        raise RuntimeError("gofile: could not parse a folder code from the link")
    try:
        from playwright.async_api import async_playwright
    except Exception as e:                          # framework image without playwright (tests/CI)
        raise RuntimeError(f"gofile: Playwright not available ({e})")

    captured: list = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="en-US", user_agent=_UA)
        page = await ctx.new_page()

        async def _on_response(resp):
            if "/contents/" in resp.url and "api.gofile.io" in resp.url:
                try:
                    captured.append(await resp.json())
                except Exception:
                    pass

        page.on("response", _on_response)
        try:
            await page.goto(f"https://gofile.io/d/{code}", wait_until="networkidle", timeout=timeout_ms)
            await page.wait_for_timeout(2500)         # let the file-manager XHR settle
            cookies = await ctx.cookies()
        finally:
            await browser.close()

    token = next((c["value"] for c in cookies if c.get("name") == "accountToken"), "")
    files, subfolders, folder_name, seen = [], 0, code, set()
    for doc in captured:
        data = doc.get("data") or {}
        folder_name = data.get("name") or folder_name
        for child in (data.get("children") or {}).values():
            if child.get("type") == "folder":
                subfolders += 1
                continue
            link = child.get("link")
            if not link or link in seen:
                continue
            seen.add(link)
            name = child.get("name") or "file"
            files.append({
                "name": name, "link": link, "size": int(child.get("size") or 0),
                "mimetype": child.get("mimetype") or "", "type": child.get("type") or "file",
                "is_video": _is_video(name, child.get("mimetype") or ""),
            })
    if not captured:
        raise RuntimeError("gofile: no contents response captured (private/expired link or page changed)")
    return {"folder": folder_name, "token": token, "files": files, "subfolders": subfolders}


async def fetch_file(link: str, token: str, dest: str, progress=None, timeout: int = 7200) -> None:
    """Stream a gofile direct link to `dest`, authenticated with the accountToken cookie."""
    headers = {"User-Agent": _UA, "Referer": "https://gofile.io/"}
    if token:
        headers["Cookie"] = f"accountToken={token}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(link, headers=headers) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as fp:
                async for chunk in r.content.iter_chunked(1 << 20):
                    fp.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        try:
                            await progress(done, total)
                        except Exception:
                            pass
