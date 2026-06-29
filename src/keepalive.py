"""Periodic keep-alive for the shared default credentials.

Some services hand out sliding tokens that lapse if unused: STING mints a short-lived access
token from its long-lived refresh token on each auth, and DSNP/Hotstar rolls its userUP forward
via the x-hs-UpdatedUserToken response header (see the DSNP service). If nobody touches a service
for long enough, the default token could go stale. Running a cheap search through the engine every
few hours exercises the real auth path - refresh for STING, token-roll+persist for DSNP - with no
duplicated logic. Only the shared default profile is kept warm; per-user accounts are the user's own.
"""
import asyncio
import os

from . import auth
from .state import engine

# Hours between keep-alive sweeps. Default 12h, comfortably inside DSNP's ~24h userUP window.
KEEPALIVE_HOURS = float(os.environ.get("KEEPALIVE_HOURS", "12"))

# service -> a throwaway query. The query result is irrelevant; the point is that the engine
# authenticates and makes one API call (which is what refreshes/rolls the token).
_TARGETS = {"STING": "a", "DSNP": "a"}


async def _ping(service: str, query: str) -> None:
    # Nothing to keep warm unless a shared default exists for this service.
    if not (auth.has_default_cookies(service) or auth.has_default_credential(service)):
        return
    cred = auth.first_credential(0, service)        # the default credential, or None for cookie services
    try:
        await engine.search(service, query, profile=auth.DEFAULT_PROFILE, credential=cred)
        print(f"[keepalive] {service}: default token refreshed")
    except Exception as e:                          # never let keep-alive crash the bot
        print(f"[keepalive] {service} skipped ({type(e).__name__}): {e}")


async def keepalive_loop() -> None:
    await asyncio.sleep(120)                         # let the bot settle after boot
    while True:
        for service, query in _TARGETS.items():
            await _ping(service, query)
        await asyncio.sleep(KEEPALIVE_HOURS * 3600)
