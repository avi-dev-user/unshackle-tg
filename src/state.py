"""Shared engine client + service catalog.

Every module that talks to the engine or reads service metadata imports from here,
so there is exactly one REST client and one cached catalog process-wide.

The catalog is *reassigned* on refresh, so it is never exported by name - reach it
only through the accessors below (a plain `from .state import _meta` would capture
the old dict and go stale after refresh()). `engine` is created once and never
rebound, so importing it by name is safe."""
from .engine import Engine

engine = Engine()                       # single REST client, shared by all modules

_services: list[dict] = []              # raw /api/services list (cached)
_meta: dict[str, dict] = {}             # tag -> full service record


async def services() -> list[dict]:
    """The service catalog, fetched once and cached. Loads meta on first call."""
    global _services, _meta
    if not _services:
        _services = await engine.services()
        _meta = {s["tag"]: s for s in _services}
    return _services


async def refresh() -> None:
    """Drop the cache and re-read from the API (picks up newly added services)."""
    global _services, _meta
    _services, _meta = [], {}
    await services()


def services_cached() -> list[dict]:
    """The already-loaded catalog (empty until services() has been awaited once)."""
    return _services


def meta(tag: str) -> dict:
    """The full service record for a tag, or an empty dict if unknown."""
    return _meta.get(tag, {})
