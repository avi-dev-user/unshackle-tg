"""Best-effort publication of downloaded subtitle files to the SubsZone API."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import aiohttp

from . import config

_SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".ttml", ".xml"}
_tasks: set[asyncio.Task] = set()


def is_subtitle(path: str) -> bool:
    return Path(path).suffix.lower() in _SUBTITLE_EXTS


def idempotency_key(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "unshackle:" + digest.hexdigest()


def _headers(**extra) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {config.SUBSZONE_API_TOKEN}", **extra}
    if config.SUBSZONE_API_HOST:
        headers["Host"] = config.SUBSZONE_API_HOST
    return headers


async def _json(response: aiohttp.ClientResponse) -> dict:
    try:
        payload = await response.json(content_type=None)
    except Exception:
        payload = {}
    if response.status >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else f"HTTP {response.status}"
        raise RuntimeError(message)
    return payload if isinstance(payload, dict) else {}


async def _poll_and_submit(upload_id: str, revision_id: str) -> None:
    base = config.SUBSZONE_API_BASE
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(config.SUBSZONE_POLL_ATTEMPTS):
            await asyncio.sleep(config.SUBSZONE_POLL_SECONDS)
            async with session.get(f"{base}/v1/uploads/{upload_id}/", headers=_headers()) as response:
                detail = await _json(response)
            revisions = detail.get("revisions") or []
            revision = next((item for item in revisions if item.get("id") == revision_id), None)
            if not revision:
                continue
            check = revision.get("check_status")
            if check in {"failed", "error"}:
                print(f"subszone inspection rejected revision {revision_id}")
                return
            if check not in {"passed", "review"}:
                continue
            if check == "review":
                print(f"subszone inspection needs review for revision {revision_id}")
                return
            body = json.dumps({"revision": revision_id}).encode()
            async with session.post(
                f"{base}/v1/uploads/{upload_id}/submit/",
                data=body,
                headers=_headers(**{"Content-Type": "application/json"}),
            ) as response:
                await _json(response)
            print(f"subszone published upload {upload_id}")
            return
        print(f"subszone inspection timed out for revision {revision_id}")


async def upload(path: str, *, release: str = "", notes: str = "") -> bool:
    """Upload one subtitle draft and detach inspection/publication polling.

    The file transfer itself is awaited so the caller may safely delete the local file afterwards.
    API or inspection failures are reported without affecting Telegram delivery.
    """
    if not config.SUBSZONE_API_BASE or not config.SUBSZONE_API_TOKEN or not is_subtitle(path):
        return False
    try:
        form = aiohttp.FormData()
        form.add_field("translation_type", "studio")
        if release:
            form.add_field("release", release[:500])
        if notes:
            form.add_field("notes", notes[:4000])
        with open(path, "rb") as handle:
            form.add_field("file", handle, filename=os.path.basename(path), content_type="application/octet-stream")
            timeout = aiohttp.ClientTimeout(total=config.SUBSZONE_UPLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{config.SUBSZONE_API_BASE}/v1/uploads/",
                    data=form,
                    headers=_headers(**{"Idempotency-Key": idempotency_key(path)}),
                ) as response:
                    payload = await _json(response)
        upload_data = payload.get("upload") or {}
        upload_id = upload_data.get("id")
        revision_id = payload.get("revision")
        if not upload_id or not revision_id:
            pending = payload.get("pending") or {}
            print(f"subszone upload awaiting title selection {pending.get('id', '')}".rstrip())
            return True
        task = asyncio.create_task(_poll_and_submit(str(upload_id), str(revision_id)))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        print(f"subszone draft uploaded {upload_id}")
        return True
    except Exception as exc:
        print(f"subszone upload failed for {os.path.basename(path)}: {type(exc).__name__}: {exc}")
        return False
