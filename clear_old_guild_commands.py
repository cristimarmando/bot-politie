import os
import asyncio
import discord
from dotenv import load_dotenv

load_dotenv()

async def main():
    token = os.getenv("DISCORD_TOKEN")
    guild_id = int(os.getenv("GUILD_ID", "0"))
    if not token or not guild_id:
        raise RuntimeError("Completează DISCORD_TOKEN și GUILD_ID în .env")

    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    @client.event
    async def on_ready():
        guild = discord.Object(id=guild_id)
        tree.clear_commands(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"Comenzi vechi șterse. Rămase: {len(synced)}")
        await client.close()

    await client.start(token)

asyncio.run(main())
