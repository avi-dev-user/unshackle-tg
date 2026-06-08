"""In-memory per-user bot state (phase 1, not persisted). Shared by import across the modules,
so only mutate these dicts, never rebind them."""

sessions: dict[int, dict] = {}      # per-user wizard state
active_jobs: dict[int, dict] = {}   # uid -> {job_id: {name, chat}} for concurrency + "my downloads"


def sess(uid: int) -> dict:
    return sessions.setdefault(uid, {})
