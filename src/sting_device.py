"""STING (Synamedia CGW) Android-TV device-flow login.

Registers an Android-TV client (`software_id il.co.stingtv.atv` -> device class
`Yes-STING-AndroidTV`, which is entitled to 1080p + Dolby Digital 5.1, unlike the
phone class's 576p / AAC 2.0), starts the OAuth device-authorization flow, and polls
for the refresh token. The user authorises by entering the shown code at the
verification URL, exactly like signing a real TV into the app.

The resulting refresh token is long-lived (~2 years) and is stored as the user's STING
credential in refresh mode.
"""

from __future__ import annotations

import asyncio

import aiohttp

CGW = "https://cgw.stingtv.co.il:9443"
SOFTWARE_ID = "il.co.stingtv.atv"  # Android-TV device class (1080p + DD5.1)
_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


async def start() -> dict:
    """Register a TV client and begin device authorization.

    Returns a dict with: client_id, device_code, user_code, verification_uri,
    interval (seconds), expires_in (seconds).
    """
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{CGW}/oauth2/register",
            json={"software_id": SOFTWARE_ID, "device_info.os_type": "Android"},
        ) as r:
            r.raise_for_status()
            client_id = (await r.json())["client_id"]
        async with s.post(f"{CGW}/oauth2/device_authorization", data={"client_id": client_id}) as r:
            r.raise_for_status()
            d = await r.json()
    return {
        "client_id": client_id,
        "device_code": d["device_code"],
        "user_code": d["user_code"],
        "verification_uri": d.get("verification_uri") or "https://ctv.stingtv.co.il",
        "interval": int(d.get("interval") or 5),
        "expires_in": int(d.get("expires_in") or 900),
    }


async def poll(client_id: str, device_code: str, interval: int, expires_in: int) -> str | None:
    """Poll the token endpoint until the user authorises.

    Returns the refresh token on success, or None on timeout / denial.
    """
    interval = max(3, int(interval))
    waited, deadline = 0, max(30, int(expires_in) - 10)
    async with aiohttp.ClientSession() as s:
        while waited < deadline:
            await asyncio.sleep(interval)
            waited += interval
            async with s.post(
                f"{CGW}/oauth2/token",
                data={"grant_type": _GRANT, "device_code": device_code, "client_id": client_id},
            ) as r:
                try:
                    j = await r.json()
                except aiohttp.ContentTypeError:
                    continue
            if r.status == 200 and j.get("refresh_token"):
                return j["refresh_token"]
            err = (j or {}).get("error", "")
            if err == "slow_down":
                interval += 5
            elif err in ("expired_token", "access_denied"):
                return None
            # "authorization_pending" (and anything else transient) -> keep polling
    return None
