from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = 36601382      # ТВОЙ api_id
api_hash = "6b52e780e40bfe028cbdf3aa47c8de58"  # ТВОЙ api_hash

with TelegramClient("account", api_id, api_hash) as client:
    print("Session created:", client.session.filename)
