"""
Async client for the unshackle REST API (`unshackle serve`).

The whole download engine is unshackle's HTTP API - this module is the only
place that talks to it. Returns parsed JSON (structured titles/tracks/jobs),
so the bot never parses CLI text. `profile` (per request) selects which user's
cookies/credentials unshackle uses → that's how per-user auth works.

Verified endpoints (unshackle 4.0):
  GET  /api/services
  POST /api/list-titles   {service, title_id, [profile], [extra...]}
  POST /api/list-tracks   {service, title_id, [wanted], [profile], ...}
  POST /api/download      {service, title_id, [profile], [wanted], [quality], ...}
  GET  /api/download/jobs/{job_id}
  DELETE /api/download/jobs/{job_id}
"""
import asyncio
from typing import Any, Optional

# Jobs are process-local already; keep their engine affinity beside the client. If the bot
# restarts, job() also has a DSNP fallback so an in-flight official-DSNP job remains visible.
_DSNP_JOBS: set[str] = set()

import aiohttp

from . import config


class UnshackleError(Exception):
    """Raised when the API returns an error payload or a non-2xx status."""


class Engine:
    def __init__(self, base_url: str = None, api_key: str = None):
        self.base = (base_url or config.UNSHACKLE_API).rstrip("/")
        self.api_key = api_key if api_key is not None else config.UNSHACKLE_API_KEY

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def _request(self, method: str, path: str, json: dict = None) -> Any:
        # Official DSNP runs on a separate Python 3.13/unshackle 6 engine. Every other
        # service stays on the existing 5.x engine. The request body is the routing key.
        service = str((json or {}).get("service") or "").upper()
        if service == "DSNP" and json is not None:
            if config.UNSHACKLE_DSNP_PROXY:
                # The bot's segment proxy may be a localhost forwarder available only in its own pod.
                # DSNP runs in a separate pod, so always replace it with that engine's residential URL.
                json["proxy"] = config.UNSHACKLE_DSNP_PROXY
            if config.UNSHACKLE_DSNP_DOWNLOAD_PROXY:
                json["proxy_download"] = config.UNSHACKLE_DSNP_DOWNLOAD_PROXY
            # The DSNP engine is a separate pod. Rewrite the bot-local job path onto a hostPath
            # mounted at the same location in both pods, so uploader.deliver can read the result.
            output_dir = json.get("output_dir")
            if output_dir:
                json["output_dir"] = "/data/dsnp-downloads/" + str(output_dir).rstrip("/").rsplit("/", 1)[-1]
        base = config.UNSHACKLE_DSNP_API if service == "DSNP" and config.UNSHACKLE_DSNP_API else self.base
        return await self._request_at(base, method, path, json)

    async def _request_at(self, base: str, method: str, path: str, json: dict = None) -> Any:
        url = f"{base}{path}"
        # bounded timeout so a hung engine can't wedge the single long-poll loop (these are
        # quick control-plane calls - submit/list/status/cancel; the download itself is polled)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
                async with s.request(method, url, json=json, headers=self._headers()) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        text = await r.text()
                        raise UnshackleError(f"{r.status}: {text[:200]}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            # surface as UnshackleError so the wizard's `except UnshackleError` shows a clean
            # "network/timeout" message (via _friendly) instead of an uncaught exception
            raise UnshackleError(f"connection to the engine timed out or failed ({type(e).__name__})")
        if isinstance(data, dict) and data.get("status") == "error":
            raise UnshackleError(data.get("message") or "unknown API error")
        return data

    # --- discovery ---
    async def services(self) -> list[dict]:
        data = await self._request("GET", "/services")
        return data.get("services", []) if isinstance(data, dict) else (data or [])

    async def list_titles(self, service: str, title_id: str, profile: str = None, **extra) -> list[dict]:
        body = {"service": service, "title_id": title_id, **extra}
        if profile:
            body["profile"] = profile
        data = await self._request("POST", "/list-titles", body)
        return data.get("titles", []) if isinstance(data, dict) else (data or [])

    async def search(self, service: str, query: str, profile: str = None,
                     credential: str = None) -> list[dict]:
        body = {"service": service, "query": query}
        if profile:
            body["profile"] = profile
        if credential:
            body["credential"] = credential
        data = await self._request("POST", "/search", body)
        return data.get("results", []) if isinstance(data, dict) else (data or [])

    async def list_tracks(self, service: str, title_id: str, wanted: str = None,
                          profile: str = None, **extra) -> dict:
        body = {"service": service, "title_id": title_id, **extra}
        if wanted:
            body["wanted"] = wanted
        if profile:
            body["profile"] = profile
        return await self._request("POST", "/list-tracks", body)

    # --- download + job tracking ---
    async def download(self, service: str, title_id: str, profile: str = None,
                       wanted: str = None, quality: list[int] = None,
                       best_available: bool = True, **extra) -> dict:
        body: dict = {"service": service, "title_id": title_id, "best_available": best_available, **extra}
        if profile:
            body["profile"] = profile
        if wanted:
            body["wanted"] = wanted
        if quality:
            body["quality"] = quality
        result = await self._request("POST", "/download", body)
        if service.upper() == "DSNP":
            job_id = result.get("job_id") or result.get("id") if isinstance(result, dict) else None
            if job_id:
                _DSNP_JOBS.add(str(job_id))
        return result

    async def job(self, job_id: str) -> dict:
        if str(job_id) in _DSNP_JOBS and config.UNSHACKLE_DSNP_API:
            return await self._request_at(config.UNSHACKLE_DSNP_API, "GET", f"/download/jobs/{job_id}")
        try:
            return await self._request("GET", f"/download/jobs/{job_id}")
        except UnshackleError as exc:
            if config.UNSHACKLE_DSNP_API and "not found" in str(exc).lower():
                return await self._request_at(config.UNSHACKLE_DSNP_API, "GET", f"/download/jobs/{job_id}")
            raise

    async def cancel(self, job_id: str) -> dict:
        if str(job_id) in _DSNP_JOBS and config.UNSHACKLE_DSNP_API:
            return await self._request_at(config.UNSHACKLE_DSNP_API, "DELETE", f"/download/jobs/{job_id}")
        return await self._request("DELETE", f"/download/jobs/{job_id}")

    async def health(self) -> Optional[dict]:
        try:
            return await self._request("GET", "/health")
        except Exception:
            return None
