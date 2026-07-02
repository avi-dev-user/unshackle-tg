"""Keep-alive logic: only pings services with a shared default, varies the query, and jitters
the interval within the configured window (kept inside DSNP's ~24h userUP TTL)."""
import asyncio

from src import auth, keepalive


def test_interval_window_inside_userup_ttl():
    # The jitter window must stay under 24h so a shared default userUP can't lapse between sweeps.
    assert 0 < keepalive.KEEPALIVE_MIN_HOURS <= keepalive.KEEPALIVE_MAX_HOURS
    assert keepalive.KEEPALIVE_MAX_HOURS < 24


def test_query_pools_are_varied_and_nonempty():
    # Not the old fixed "a": each targeted service offers several natural terms to choose from.
    for service in ("STING", "DSNP"):
        pool = keepalive._QUERIES[service]
        assert len(pool) >= 3 and pool != ["a"]
    assert keepalive._DEFAULT_QUERIES


def test_ping_skips_service_without_default(monkeypatch):
    # No shared default -> no engine call at all (per-user accounts are the user's own).
    monkeypatch.setattr(auth, "has_default_cookies", lambda s: False)
    monkeypatch.setattr(auth, "has_default_credential", lambda s: False)
    called = []
    async def _boom(*a, **k):
        called.append(1)
    monkeypatch.setattr(keepalive.engine, "search", _boom)
    asyncio.run(keepalive._ping("DSNP"))
    assert called == []


def test_ping_searches_with_default_and_pool_query(monkeypatch):
    monkeypatch.setattr(auth, "has_default_cookies", lambda s: True)
    monkeypatch.setattr(auth, "has_default_credential", lambda s: False)
    monkeypatch.setattr(auth, "first_credential", lambda uid, s: None)
    seen = {}
    async def _search(service, query, profile=None, credential=None):
        seen.update(service=service, query=query, profile=profile)
    monkeypatch.setattr(keepalive.engine, "search", _search)
    asyncio.run(keepalive._ping("DSNP"))
    assert seen["service"] == "DSNP"
    assert seen["profile"] == auth.DEFAULT_PROFILE
    assert seen["query"] in keepalive._QUERIES["DSNP"]
