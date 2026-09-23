#!/usr/bin/env python3
"""
Run by the "sync-discord-config" GitHub Action. Reads the config channel
using a Discord bot token (from the DISCORD_BOT_TOKEN environment variable -
set as a GitHub Actions secret, never committed) and writes the result as a
plain public file, mods.json, that Artisan-Installer.py fetches at runtime
with no token at all.

Required environment variables:
  DISCORD_BOT_TOKEN    the bot token (GitHub Actions secret)
  DISCORD_CHANNEL_ID   the config channel's ID (can be a secret or a plain
                       repository variable - it isn't sensitive by itself)

Usage: python sync_discord_config.py [output_path]
       (output_path defaults to mods.json next to this script)
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

# The name of the installer file in this repo, imported below to reuse its
# fetch_discord_messages() / parse_messages() so the message format only has
# to be defined in one place.
INSTALLER_FILENAME = "Artisan-Installer.py"


def load_installer_module():
    path = Path(__file__).resolve().parent / INSTALLER_FILENAME
    if not path.is_file():
        sys.exit(f"error: couldn't find {INSTALLER_FILENAME} next to this script (looked in {path})")
    spec = importlib.util.spec_from_file_location("artisan_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)   # safe: the installer only runs main() under __main__
    return module


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel:
        sys.exit("error: DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must both be set")

    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "mods.json"

    installer = load_installer_module()

    try:
        messages = installer.fetch_discord_messages(token, channel)
    except installer.ConfigError as e:
        sys.exit(f"error: could not read Discord ({e})")

    version, mods, removals, warnings = installer.parse_messages(messages)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if not mods and not removals:
        sys.exit("error: no !required, !optional or !remove messages found - "
                 "not overwriting the published config with an empty one")

    data = {
        "mc_version": version or installer.DEFAULT_MC_VERSION,
        "mods": [
            {
                "slug": m.slug,
                "name": m.name,
                "note": m.note,
                "required": m.required,
                "default": m.default,
            }
            for m in mods
        ],
        "removals": removals,
    }

    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}: {len(mods)} mod(s), {len(removals)} removal(s), "
         f"Minecraft {data['mc_version']}")


if __name__ == "__main__":
    main()
