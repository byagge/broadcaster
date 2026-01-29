from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = 31962782      # ТВОЙ api_id
api_hash = "6f00f48bfbac9da1b04594a326b51b89"  # ТВОЙ api_hash

with TelegramClient("account", api_id, api_hash) as client:
    print("Session created:", client.session.filename)
