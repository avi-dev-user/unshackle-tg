"""Pytest setup: point all data dirs at throwaway temp dirs and provide an encryption key
BEFORE any `src.*` import (config reads these from the environment at import time)."""
import os
import tempfile

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="ushtest_state_"))
os.environ.setdefault("COOKIES_DIR", tempfile.mkdtemp(prefix="ushtest_cookies_"))
os.environ.setdefault("WVD_DIR", tempfile.mkdtemp(prefix="ushtest_wvd_"))
os.environ.setdefault("DOWNLOADS_DIR", tempfile.mkdtemp(prefix="ushtest_dl_"))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
except ImportError:
    pass
