#!/usr/bin/env python3
"""Quick script to send a voice message to Discord DM."""

import asyncio
import os
import sys
import yaml

sys.path.insert(0, os.path.dirname(__file__))

from gateway.platforms.discord import DiscordAdapter
from gateway.config import PlatformConfig


async def main():
    # Load config
    config_path = os.path.expanduser("~/.hermes/config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    discord_cfg = config.get("discord", {})
    token = discord_cfg.get("token") or os.getenv("DISCORD_BOT_TOKEN")
    
    if not token:
        print("No Discord token found!")
        return
    
    # Create adapter
    platform_config = PlatformConfig(
        name="discord",
        token=token,
        enabled=True,
    )
    
    adapter = DiscordAdapter(platform_config)
    
    # Connect
    print("Connecting to Discord...")
    success = await adapter.connect()
    if not success:
        print("Failed to connect!")
        return
    
    print("Connected! Sending voice message...")
    
    # Send voice to user (DM)
    # The user's DM channel ID from context is 1476751139071328349
    audio_path = "/home/gille/.hermes/audio_cache/tts_20260321_132600.ogg"
    
    result = await adapter.send_voice(
        chat_id="1476751139071328349",
        audio_path=audio_path,
    )
    
    print(f"Result: {result}")
    
    await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())