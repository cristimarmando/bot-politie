import os
from dotenv import load_dotenv
from cco.main import bot, start_dashboard_thread

load_dotenv()

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Lipsește DISCORD_TOKEN din .env")
    start_dashboard_thread()
    bot.run(token)
