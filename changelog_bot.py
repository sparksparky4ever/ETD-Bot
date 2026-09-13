"""
Patch notes Discord bot.

Setup:
  pip install -r requirements.txt
  export DISCORD_BOT_TOKEN="your-bot-token"
  export CHANGELOG_CHANNEL_ID="123456789012345678"   # channel new updates get posted to
  python changelog_bot.py

Slash commands:
  /post-update version:1.4.0 title:"Combat rebalance"
      -> opens a modal, one change per line: "Added: new boss"
  /changelog [count]         -> shows the most recent updates
  /changelog-version version -> shows one specific version
  /delete-update version     -> removes an entry (Manage Server permission required)

Data is stored in changelog_data.json next to this file. No external DB required.
"""

import os
import json
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("changelog_bot")

DATA_PATH = Path(__file__).parent / "changelog_data.json"
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CHANNEL_ID_RAW = os.environ.get("CHANGELOG_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")

CHANGELOG_CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW else None

TAG_COLORS = {
    "Added": 0x5FBE8B,
    "Changed": 0x5B9BD9,
    "Fixed": 0xE0A64C,
    "Removed": 0xD9695B,
}
TAG_EMOJI = {
    "Added": "🟢",
    "Changed": "🔵",
    "Fixed": "🟠",
    "Removed": "🔴",
}
VALID_TAGS = set(TAG_COLORS)


def load_entries() -> list[dict]:
    if not DATA_PATH.exists():
        return []
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.exception("Failed reading %s, starting from empty state", DATA_PATH)
        return []


def save_entries(entries: list[dict]) -> None:
    DATA_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def parse_change_lines(raw: str) -> list[dict]:
    """Turns modal textarea input into structured change entries.
    Each line is either "Tag: text" or plain text (defaults to Changed)."""
    changes = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        tag, sep, text = line.partition(":")
        tag = tag.strip().title()
        if sep and tag in VALID_TAGS:
            changes.append({"type": tag, "text": text.strip()})
        else:
            changes.append({"type": "Changed", "text": line})
    return changes


def build_embed(entry: dict) -> discord.Embed:
    dominant_tag = entry["changes"][0]["type"] if entry["changes"] else "Changed"
    embed = discord.Embed(
        title=f"v{entry['version']}" + (f" — {entry['title']}" if entry.get("title") else ""),
        color=TAG_COLORS.get(dominant_tag, 0x5B9BD9),
        timestamp=datetime.fromtimestamp(entry["created_at"], tz=timezone.utc),
    )
    lines = [f"{TAG_EMOJI.get(c['type'], '⚪')} **{c['type']}** — {c['text']}" for c in entry["changes"]]
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Posted by {entry.get('author', 'unknown')}")
    return embed


class ChangeModal(discord.ui.Modal, title="Post update"):
    changes = discord.ui.TextInput(
        label="Changes (one per line: Tag: text)",
        style=discord.TextStyle.paragraph,
        placeholder="Added: new boss fight\nFixed: crash on alt-tab\nChanged: reduced fall damage",
        required=True,
        max_length=1800,
    )

    def __init__(self, version: str, title: str | None):
        super().__init__()
        self.version = version
        self.title_text = title

    async def on_submit(self, interaction: discord.Interaction):
        parsed = parse_change_lines(str(self.changes.value))
        if not parsed:
            await interaction.response.send_message("No valid change lines found — nothing posted.", ephemeral=True)
            return

        entries = load_entries()
        entry = {
            "id": uuid.uuid4().hex[:8],
            "version": self.version,
            "title": self.title_text,
            "changes": parsed,
            "author": str(interaction.user),
            "created_at": time.time(),
        }
        entries.append(entry)
        save_entries(entries)

        embed = build_embed(entry)
        target_channel = interaction.channel
        if CHANGELOG_CHANNEL_ID:
            configured = interaction.client.get_channel(CHANGELOG_CHANNEL_ID)
            if configured is not None:
                target_channel = configured

        await target_channel.send(embed=embed)
        if target_channel.id != interaction.channel_id:
            await interaction.response.send_message(f"Posted to {target_channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message("Posted.", ephemeral=True)


class ChangelogBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = ChangelogBot()


@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)


@client.tree.command(name="post-update", description="Post a new patch note")
@app_commands.describe(version="Version number, e.g. 1.4.0", title="Optional headline for this update")
async def post_update(interaction: discord.Interaction, version: str, title: str | None = None):
    await interaction.response.send_modal(ChangeModal(version=version, title=title))


@client.tree.command(name="changelog", description="Show the most recent updates")
@app_commands.describe(count="How many updates to show (default 3, max 10)")
async def changelog(interaction: discord.Interaction, count: app_commands.Range[int, 1, 10] = 3):
    entries = sorted(load_entries(), key=lambda e: e["created_at"], reverse=True)[:count]
    if not entries:
        await interaction.response.send_message("No updates posted yet.", ephemeral=True)
        return
    await interaction.response.send_message(embeds=[build_embed(e) for e in entries])


@client.tree.command(name="changelog-version", description="Show a specific version's patch notes")
@app_commands.describe(version="Version number to look up")
async def changelog_version(interaction: discord.Interaction, version: str):
    entries = [e for e in load_entries() if e["version"] == version]
    if not entries:
        await interaction.response.send_message(f"No entry found for version {version}.", ephemeral=True)
        return
    await interaction.response.send_message(embeds=[build_embed(e) for e in entries])


@client.tree.command(name="delete-update", description="Remove a patch note by version")
@app_commands.describe(version="Version number to delete")
@app_commands.checks.has_permissions(manage_guild=True)
async def delete_update(interaction: discord.Interaction, version: str):
    entries = load_entries()
    remaining = [e for e in entries if e["version"] != version]
    if len(remaining) == len(entries):
        await interaction.response.send_message(f"No entry found for version {version}.", ephemeral=True)
        return
    save_entries(remaining)
    await interaction.response.send_message(f"Removed update {version}.", ephemeral=True)


@delete_update.error
async def delete_update_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Server permission to delete updates.", ephemeral=True)
    else:
        log.exception("Unhandled error in delete-update", exc_info=error)
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
