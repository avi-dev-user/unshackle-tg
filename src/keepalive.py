"""Periodic keep-alive for the shared default credentials.

Some services hand out sliding tokens that lapse if unused: STING mints a short-lived access
token from its long-lived refresh token on each auth, and DSNP/Hotstar rolls its userUP forward
via the x-hs-UpdatedUserToken response header (see the DSNP service). If nobody touches a service
for long enough, the default token could go stale. Running a cheap search through the engine every
few hours exercises the real auth path - refresh for STING, token-roll+persist for DSNP - with no
duplicated logic. Only the shared default profile is kept warm; per-user accounts are the user's own.

To avoid looking like an automated heartbeat, the interval is jittered (not a fixed period), the
services are hit in a random order with a short random gap between them, and each ping uses a
different, natural-looking search term instead of a fixed placeholder.
"""
import asyncio
import os
import random

from . import auth
from .state import engine

# Interval between keep-alive sweeps, drawn fresh each cycle from [MIN, MAX] hours so the timing
# isn't a fixed period. Defaults stay comfortably inside DSNP's ~24h userUP window. A legacy
# fixed KEEPALIVE_HOURS still works: if set, it centres the window (+/- 2h).
_legacy = os.environ.get("KEEPALIVE_HOURS")
if _legacy:
    _c = float(_legacy)
    KEEPALIVE_MIN_HOURS = float(os.environ.get("KEEPALIVE_MIN_HOURS", max(1.0, _c - 2)))
    KEEPALIVE_MAX_HOURS = float(os.environ.get("KEEPALIVE_MAX_HOURS", _c + 2))
else:
    KEEPALIVE_MIN_HOURS = float(os.environ.get("KEEPALIVE_MIN_HOURS", "9"))
    KEEPALIVE_MAX_HOURS = float(os.environ.get("KEEPALIVE_MAX_HOURS", "15"))

# Per-service pools of natural search terms. The result is irrelevant - the point is to make one
# authenticated API call - but varying the term (vs a fixed "a") keeps the traffic from looking
# like a scripted probe. DSNP is Disney+/Hotstar (English + Indian catalogue); STING is Israeli.
_QUERIES = {
    "STING": ["פאודה", "שטיסל", "טהרן", "חדשות", "משפחה", "סרט", "קומדיה", "דרמה", "ילדים", "אקשן"],
    "DSNP": ["loki", "marvel", "avatar", "star wars", "moana", "simpsons", "spider", "frozen",
             "avengers", "encanto"],
}
_DEFAULT_QUERIES = ["the", "love", "story", "night", "home"]


async def _ping(service: str) -> None:
    # Nothing to keep warm unless a shared default exists for this service.
    if not (auth.has_default_cookies(service) or auth.has_default_credential(service)):
        return
    query = random.choice(_QUERIES.get(service) or _DEFAULT_QUERIES)
    cred = auth.first_credential(0, service)        # the default credential, or None for cookie services
    try:
        await engine.search(service, query, profile=auth.DEFAULT_PROFILE, credential=cred)
        print(f"[keepalive] {service}: default token refreshed")
    except Exception as e:                          # never let keep-alive crash the bot
        print(f"[keepalive] {service} skipped ({type(e).__name__}): {e}")


async def keepalive_loop() -> None:
    await asyncio.sleep(random.uniform(90, 240))     # let the bot settle after boot (jittered)
    while True:
        services = list(_QUERIES)
        random.shuffle(services)                     # no fixed order across services
        for i, service in enumerate(services):
            if i:                                    # small random gap between services (not in lockstep)
                await asyncio.sleep(random.uniform(30, 180))
            await _ping(service)
        hours = random.uniform(KEEPALIVE_MIN_HOURS, KEEPALIVE_MAX_HOURS)
        await asyncio.sleep(hours * 3600)
