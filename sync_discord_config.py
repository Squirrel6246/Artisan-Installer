#!/usr/bin/env python3
"""
Run by the "sync-discord-config" GitHub Action. Reads one or more Discord
channels using a bot token (from the DISCORD_BOT_TOKEN environment variable -
set as a GitHub Actions secret, never committed) and writes the result as a
plain public file, mods.json, that Artisan-Installer.py fetches at runtime
with no token at all.

This script is fully standalone: it does not import Artisan-Installer.py, so
it has its own copies of the Discord-fetching and message-parsing logic. If
you change the message format in one file, update the other to match.

Required environment variable:
  DISCORD_BOT_TOKEN   the bot token (GitHub Actions secret)

Usage: python sync_discord_config.py [output_path]
       (output_path defaults to mods.json next to this script)

Message format (one message per item; first line is the command):

    !version 26.2

    !required
    slug: fabric-api
    name: Fabric API
    note: What this mod is, in one or two sentences.

    !optional
    slug: simple-voice-chat
    name: Simple Voice Chat
    note: What this mod is, shown before asking.
    default: yes

    !remove sodium

Multiple channels, merged: list several channels in CHANNELS below. A
channel's "kind" is the command implied for a message in that channel which
does NOT start with "!" - so a channel dedicated to (say) optional mods can
just be posted "slug: sodium\\nnote: ..." without repeating "!optional"
every time. A message that DOES start with "!" always uses that explicit
command, in any channel, which still lets you e.g. post "!remove sodium" in
an otherwise "optional" channel. All configured channels are merged into one
timeline ordered by Discord message ID (which is chronological), so "the
newest message wins" for a repeated slug still holds true across channels,
not just within one.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# =====================================================================
#  SETTINGS
# =====================================================================

# Each entry is a channel to read, merged together with all the others.
# "kind" is the implied command for a message that doesn't start with "!" in
# that channel: "required", "optional", "remove", "version", or None (that
# channel's messages must always start with "!command" themselves - this
# matches the single-channel format from earlier versions of this script).
CHANNELS = [
    {"id": "1551627558473171004", "kind": None},
    {"id": "1552448514896822382", "kind": "required"},
    {"id": "1552448544617537537", "kind": "optional"},
    {"id": "1553235416579047515", "kind": "remove"},
    # Example of splitting mods by channel instead of by "!command" line:
    # {"id": "<required-mods channel id>", "kind": "required"},
    # {"id": "<optional-mods channel id>", "kind": "optional"},
    # {"id": "<removals channel id>",      "kind": "remove"},
]

# Only obey messages from these Discord user IDs, across every channel above.
# Empty list = obey anyone who can post in a configured channel.
DISCORD_ALLOWED_AUTHOR_IDS = []

DEFAULT_MC_VERSION = "26.2"   # used if no channel has a !version message
DISCORD_UA = "DiscordBot (https://github.com/Squirrel6246/Artisan-Installer, sync-script)"

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,63}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}([-+][A-Za-z0-9_.\-]+)?$")
FIELD_KEYS = {"slug", "name", "note", "default"}


class ConfigError(Exception):
    pass


def clean(text):
    """Strip control characters so message text can't mess with anything downstream."""
    return re.sub(r"[\x00-\x1f\x7f]", "", text).strip()


def _is_no(text):
    return text.strip().lower() in ("no", "n", "false", "0", "off")


# ---------------------------------------------------------------------
#  Reading Discord
# ---------------------------------------------------------------------

def fetch_discord_messages(token, channel_id):
    """Return (message_id: int, content: str) pairs from the channel, oldest first."""
    raw, before = [], None
    while len(raw) < 500:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100"
        if before:
            url += f"&before={before}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bot {token}", "User-Agent": DISCORD_UA}
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    batch = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 1:       # rate limited: wait once, retry
                    time.sleep(min(float(e.headers.get("Retry-After", 2)), 10))
                    continue
                reasons = {
                    401: "the bot token is wrong or has been reset",
                    403: "the bot can't read that channel (it needs View Channel + "
                         "Read Message History)",
                    404: "channel ID not found",
                }
                raise ConfigError(reasons.get(e.code, f"Discord returned HTTP {e.code}"))
            except urllib.error.URLError as e:
                raise ConfigError(f"could not reach Discord ({e.reason})")
        if not batch:
            break
        raw.extend(batch)
        before = batch[-1]["id"]
        if len(batch) < 100:
            break

    if raw and not any(m.get("content") for m in raw):
        raise ConfigError(
            "Discord returned messages with no text. Turn on 'Message Content Intent' "
            "for the bot in the Discord Developer Portal."
        )
    if DISCORD_ALLOWED_AUTHOR_IDS:
        allowed = {str(a) for a in DISCORD_ALLOWED_AUTHOR_IDS}
        raw = [m for m in raw if str(m.get("author", {}).get("id")) in allowed]
    raw.reverse()   # Discord returns newest first
    return [(int(m["id"]), m.get("content", "")) for m in raw]


def apply_channel_default(text, kind):
    """
    If `text` doesn't already start with "!", prefix it with the command
    implied by this channel's `kind`, so a plain field block (or a plain
    slug, for a removals channel) can be posted without typing "!required" /
    "!remove" / etc. every time. A message that already starts with "!" is
    left untouched - an explicit command always wins, in any channel.
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("!") or not kind:
        return text
    if kind == "remove":
        first_line, _, rest = text.partition("\n")
        return f"!remove {first_line.strip()}" + (("\n" + rest) if rest else "")
    if kind == "version":
        return f"!version {stripped}"
    if kind in ("required", "optional"):
        return f"!{kind}\n{text}"
    return text


# ---------------------------------------------------------------------
#  Parsing (same message format as Artisan-Installer.py's parse_messages)
# ---------------------------------------------------------------------

def parse_messages(contents):
    """
    Turn Discord message texts (oldest first, across all merged channels)
    into (mc_version, mods, removals, warnings). `mods` is a list of plain
    dicts: {"slug", "name", "note", "required", "default"}.
    Ordinary chat messages (not starting with "!") are ignored silently.
    If the same slug appears in several messages, the newest one wins - so a
    later !remove beats an earlier !required, and vice versa.
    """
    version = None
    mods = {}        # slug.lower() -> mod dict
    removals = {}    # slug.lower() -> slug
    warnings = []

    for n, text in enumerate(contents, 1):
        lines = [l.strip() for l in text.splitlines() if not l.strip().startswith("```")]
        lines = [l for l in lines if l]
        if not lines or not lines[0].startswith("!"):
            continue

        head = lines[0][1:].split(None, 1)
        command = head[0].lower() if head else ""
        arg = head[1].strip() if len(head) > 1 else ""

        if command == "version":
            candidate = clean(arg or (lines[1] if len(lines) > 1 else ""))
            if VERSION_RE.match(candidate):
                version = candidate
            else:
                warnings.append(f"Message {n}: '{candidate}' is not a valid Minecraft version.")

        elif command in ("required", "optional"):
            fields, last = {}, None
            for line in lines[1:]:
                m = re.match(r"^([A-Za-z]+)\s*:\s*(.*)$", line)
                if m and m.group(1).lower() in FIELD_KEYS:
                    last = m.group(1).lower()
                    fields[last] = m.group(2).strip()
                elif last in ("name", "note"):
                    fields[last] += " " + line      # continuation of a long note

            slug = clean(fields.get("slug", ""))
            if not SLUG_RE.match(slug):
                warnings.append(f"Message {n} (!{command}): missing or invalid 'slug:' line.")
                continue
            mod = {
                "slug": slug,
                "name": clean(fields.get("name", "")) or slug,
                "note": clean(fields.get("note", "")),
                "required": command == "required",
                "default": not _is_no(fields.get("default", "yes")),
            }
            removals.pop(slug.lower(), None)
            mods.pop(slug.lower(), None)   # newest message wins, and keeps its position
            mods[slug.lower()] = mod

        elif command == "remove":
            candidates = re.split(r"[,\s]+", arg) if arg else []
            for line in lines[1:]:
                m = re.match(r"^slug\s*:\s*(.*)$", line, re.I)
                if m:
                    candidates += re.split(r"[,\s]+", m.group(1).strip())
            candidates = [clean(c) for c in candidates if clean(c)]
            if not candidates:
                warnings.append(f"Message {n} (!remove): no slug given. Use '!remove <slug>'.")
                continue
            for slug in candidates:
                if not SLUG_RE.match(slug):
                    warnings.append(f"Message {n} (!remove): '{slug}' is not a valid slug.")
                    continue
                mods.pop(slug.lower(), None)
                removals.pop(slug.lower(), None)
                removals[slug.lower()] = slug

        else:
            warnings.append(f"Message {n}: unknown command '!{command}' (ignored).")

    return version, list(mods.values()), list(removals.values()), warnings


# ---------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------

def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("error: DISCORD_BOT_TOKEN must be set")

    channels = CHANNELS
    env_channel = os.environ.get("DISCORD_CHANNEL_ID")   # simple single-channel override
    if env_channel and not channels:
        channels = [{"id": env_channel, "kind": None}]
    if not channels:
        sys.exit("error: no channels configured - set CHANNELS in this script, or the "
                 "DISCORD_CHANNEL_ID environment variable for a single channel")

    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "mods.json"

    tagged = []   # (message_id, content) across every channel, merged
    for ch in channels:
        try:
            for message_id, content in fetch_discord_messages(token, ch["id"]):
                tagged.append((message_id, apply_channel_default(content, ch.get("kind"))))
        except ConfigError as e:
            sys.exit(f"error: could not read channel {ch['id']} ({e})")

    tagged.sort(key=lambda pair: pair[0])   # message IDs are chronological across all channels
    contents = [content for _, content in tagged]

    version, mods, removals, warnings = parse_messages(contents)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if not mods and not removals:
        sys.exit("error: no !required, !optional or !remove messages found across "
                 f"{len(channels)} channel(s) - not overwriting the published config "
                 "with an empty one")

    data = {
        "mc_version": version or DEFAULT_MC_VERSION,
        "mods": mods,
        "removals": removals,
        "synced_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }

    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}: {len(mods)} mod(s), {len(removals)} removal(s), "
         f"Minecraft {data['mc_version']}, from {len(channels)} channel(s)")


if __name__ == "__main__":
    main()
