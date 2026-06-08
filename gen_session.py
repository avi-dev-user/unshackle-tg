"""Run once to create a Premium-account session string for >2GB uploads:
    python gen_session.py
Enter the Premium account's phone + the login code. Paste the printed string
into .env as PREMIUM_SESSION. Use a throwaway/secondary account if you prefer.

The login shows up in that account's Settings → Devices as the clear name below,
so you can recognise and revoke it anytime."""
import os

from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

with Client(
    "gen",
    api_id=int(os.environ["API_ID"]),
    api_hash=os.environ["API_HASH"],
    in_memory=True,
    app_version="unshackle-bot uploader",      # shown in Active Sessions
    device_model="unshackle-bot (4GB uploader)",
    system_version="downloader",
) as app:
    me = app.get_me()
    print(f"\nLogged in as: {me.first_name} (@{me.username}) | Premium: {getattr(me, 'is_premium', '?')}")
    print("\n⚠️  The string below is a FULL login to this account - treat it like a password. "
          "Run this only in a private terminal; never paste it into logs, chats, or CI.")
    print("\n=== PREMIUM_SESSION ===\n" + app.export_session_string() + "\n")
