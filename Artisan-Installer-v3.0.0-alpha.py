#!/usr/bin/env python3
"""
Fabric + mods installer for Minecraft.

What it does
  1. Checks for a newer release of this installer on GitHub (optional).
  2. Loads its mod list from CONFIG_URL - a plain public file kept in sync
     with a Discord channel by a scheduled GitHub Action (see
     sync_discord_config.py and .github/workflows/sync-discord-config.yml)
     - or from the built-in list below if that isn't reachable.
  3. Shows a summary and lets the user stop before anything happens.
  4. Asks about each optional mod (nothing is installed until all are answered).
  5. Checks what is already installed: the Fabric loader for the needed
     Minecraft version, and the version of every mod in the mods folder.
  6. Installs/updates only what needs it, removes mods dropped from the pack,
     and prints a final report.

No Discord bot token ever ships in this file: this installer only ever makes
a plain, unauthenticated GET request. The token lives solely as a GitHub
Actions secret, used by sync_discord_config.py to read Discord and publish
CONFIG_URL. Setting MODINSTALLER_DISCORD_TOKEN / MODINSTALLER_DISCORD_CHANNEL
as local environment variables makes this script read Discord live instead,
for testing the message format before the next sync runs.

Command-line options
  --check             Load and print the mod list (and any config-loading
                      problems) without installing anything.
  --update            Check GitHub for a newer release and update this script.
  --all               Install every optional mod automatically, without asking.
  --no-update-check   Skip the automatic update check at startup.
  --no-color          Turn off colors (the NO_COLOR environment variable works too).
  --help              Show this list.

Discord message format (read by sync_discord_config.py; one message per item,
first line is the command):

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
"""
import sys
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


# Fallback if launched in an environment without a standard input stream
if sys.stdin is None:
    sys.stdin = io.StringIO()
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()


# Force Windows to allocate a console window if launched without one
if sys.platform == "win32":
    # Allocate console
    ctypes.windll.kernel32.AllocConsole()
    
    # Redirect stdio to the newly created console
    sys.stdin = open("CONIN$", "r")
    sys.stdout = open("CONOUT$", "w")
    sys.stderr = open("CONOUT$", "w")


@dataclass
class Mod:
    slug: str                 # the name in the Modrinth URL: modrinth.com/mod/<slug>
    name: str = ""            # friendly name shown to the user (defaults to the slug)
    note: str = ""            # what the mod is / why it's needed
    required: bool = True     # True = always installed, False = user is asked
    default: bool = True      # optional mods only: the answer if the user just presses Enter

    def __post_init__(self):
        self.name = self.name or self.slug


# =====================================================================
#  SETTINGS - edit this section
# =====================================================================

# --- Who you are. Sent as the User-Agent so Modrinth/GitHub/Discord can identify
#     this program (and contact you instead of blocking it if it misbehaves).
GITHUB_USER = "Squirrel6246"        # your GitHub / Modrinth / Discord username
PROJECT_NAME = "Artisan-Installer"   # what you call this program
VERSION = "1.0.1"                 # BUMP THIS for every release (the updater compares it)
CONTACT = "Jackcech66@gmail.com"     # an email address or a website
PROJECT_URL = "https://github.com/Squirrel6246/Artisan-Installer/tree/Squirrel6246-discord-fetch"  # your GitHub repo (used for updates)

MODRINTH_UA = f"{GITHUB_USER}/{PROJECT_NAME}/{VERSION} ({CONTACT})"
DISCORD_UA = f"DiscordBot ({PROJECT_URL}, {VERSION})"   # Discord requires this exact shape

# --- Self-update from GitHub releases.
CHECK_FOR_UPDATES = True             # check at every startup (--no-update-check skips it)
RELEASE_SCRIPT_NAME = "Artisan-Installer.exe" # the name of the .exe asset on your GitHub
                                     # releases (used to self-update a compiled build);
                                     # also doubles as the raw-script filename for --check/
                                     # source installs, if you ever publish that too.

# --- Where the mod list normally comes from: a plain JSON file kept in sync
#     with Discord by a scheduled GitHub Action (see .github/workflows/
#     sync-discord-config.yml and sync_discord_config.py). This is a public,
#     non-secret URL - no bot token is ever shipped in this script.
CONFIG_URL = "https://raw.githubusercontent.com/Squirrel6246/Artisan-Installer/tree/Squirrel6246-discord-fetch/main/mods.json"
DISCORD_SOURCE_LABEL = "Artisan SMP Discord"   # shown to the user as "Mod list loaded from:"

# --- Local testing only: set these two environment variables to read a
#     Discord channel live instead of the published file above. Never put a
#     real token in this script - use MODINSTALLER_DISCORD_TOKEN /
#     MODINSTALLER_DISCORD_CHANNEL instead, and keep the real token only as a
#     GitHub Actions secret (used by sync_discord_config.py, not this file).
#     Extra safety for both paths: only obey messages from these Discord user
#     IDs. Empty list = obey anyone who can post in the channel.
DISCORD_ALLOWED_AUTHOR_IDS = []

# --- Built-in list, used when Discord isn't configured or can't be reached.
DEFAULT_MC_VERSION = "26.2"
DEFAULT_MODS = [
    Mod(
        slug="fabric-api",
        name="Fabric API",
        note="Core library that almost every Fabric mod needs in order to run.",
        required=True,
    ),
    Mod(
        slug="simple-voice-chat",
        name="Simple Voice Chat",
        note="Proximity voice chat: you hear players who are close to you. The server "
             "must have it too, and everyone who wants to talk needs it installed.",
        required=False,
        default=True,
    ),
]
DEFAULT_REMOVALS = []   # Modrinth slugs of mods to delete if found, e.g. ["sodium"]

# =====================================================================
#  END OF SETTINGS
# =====================================================================

WIDTH = 78
BAR_WIDTH = 20
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,63}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}([-+][A-Za-z0-9_.\-]+)?$")
FIELD_KEYS = {"slug", "name", "note", "default"}


class ConfigError(Exception):
    pass


class UpdateError(Exception):
    pass


_SKIP_EXIT_PROMPT = False  # set when a Windows update helper is about to relaunch us


@dataclass
class Config:
    mc_version: str
    mods: list
    source: str
    warnings: list = field(default_factory=list)
    removals: list = field(default_factory=list)   # slugs of mods to delete


@dataclass
class Result:
    name: str
    group: str           # "loader" | "required" | "optional" | "removal"
    status: str          # key of STATUS below
    detail: str = ""


# ---------------------------------------------------------------------
#  Terminal: colors, wrapped text, spinner, progress bar
# ---------------------------------------------------------------------

IS_TTY = False
USE_COLOR = False

_CODES = {
    "red": "31",
    "green": "32",
    "blue": "94",
    "bright_blue": "38;5;33",
    "yellow": "33",
    "bright_yellow": "38;5;226",
    "orange": "38;5;208",
    "dim": "2",
    "white": "37",
}

# status key -> (label, color) for the final report
STATUS = {
    "installed":  ("INSTALLED", "green"),
    "updated":    ("UPDATED", "green"),
    "up to date": ("UP TO DATE", "blue"),
    "removed":    ("REMOVED", "yellow"),
    "skipped":    ("SKIPPED", "yellow"),
    "not found":  ("NOT FOUND", "dim"),
    "failed":     ("FAILED", "red"),
    "not run":    ("NOT RUN", "orange"),
}
LABEL_WIDTH = 13   # len("[UNAVAILABLE]")


def _enable_windows_ansi():
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def init_terminal(no_color=False):
    """Decide whether we're on a real terminal and whether colors are safe."""
    global IS_TTY, USE_COLOR
    try:
        IS_TTY = bool(sys.stdout and sys.stdout.isatty())
    except Exception:
        IS_TTY = False
    color = IS_TTY and not no_color and "NO_COLOR" not in os.environ \
        and os.environ.get("TERM") != "dumb"
    if color and sys.platform == "win32":
        color = _enable_windows_ansi()
    USE_COLOR = color


def paint(text, color=None, bold=False):
    if not USE_COLOR or not (color or bold):
        return text
    codes = []
    if bold:
        codes.append("1")
    if color:
        codes.append(_CODES[color])
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def say(text="", indent=0, hang=0, color=None, bold=False):
    """Print wrapped text. `hang` indents wrapped lines further (numbered steps)."""
    if not text:
        print()
        return
    wrapped = textwrap.fill(text, width=WIDTH, initial_indent=" " * indent,
                            subsequent_indent=" " * (indent + hang))
    print("\n".join(paint(line, color, bold) for line in wrapped.split("\n")))


def rule(title=None):
    print(paint("=" * WIDTH, "dim"))
    if title:
        print("  " + paint(title, bold=True))
        print(paint("=" * WIDTH, "dim"))


def status_line(label, color, text, indent=2, detail=None):
    """[LABEL] text   - with the label colored. Optional dim detail line below."""
    tag = f"[{label}]".ljust(LABEL_WIDTH)
    prefix = " " * indent + tag + " "
    body = textwrap.fill(text or "-", width=WIDTH, initial_indent=prefix,
                         subsequent_indent=" " * len(prefix))
    first, *rest = body.split("\n")
    first = first.replace(tag, paint(tag, color, bold=True), 1)
    print("\n".join([first] + rest))
    if detail:
        say(detail, indent=len(prefix), color="dim")


class Spinner:
    """`with Spinner("Doing a thing"):` - animated on a terminal, one line otherwise."""
    FRAMES = "|/-\\"

    def __init__(self, message):
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if IS_TTY:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"  {self.message}...")
        return self

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = paint(self.FRAMES[i % len(self.FRAMES)], "yellow")
            sys.stdout.write(f"\r  {frame} {self.message}...")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.1)

    def __exit__(self, *exc):
        if self._thread:
            self._stop.set()
            self._thread.join()
            sys.stdout.write("\r" + " " * (WIDTH - 1) + "\r")
            sys.stdout.flush()
        return False


class ProgressBar:
    """Progress bar for downloads (unit='bytes') or counted work (unit='items')."""

    def __init__(self, label, total=None, unit="bytes"):
        self.label = label[:26]
        self.total = total
        self.unit = unit
        self._last = 0.0
        if not IS_TTY:
            print(f"  {label}...")

    def _render(self, done):
        label = self.label.ljust(26)
        if self.total:
            frac = min(done / self.total, 1.0)
            filled = int(frac * BAR_WIDTH)
            bar = paint("#" * filled, "green") + "-" * (BAR_WIDTH - filled)
            if self.unit == "bytes":
                amount = f"{done / 1048576:.1f}/{self.total / 1048576:.1f} MB"
            else:
                amount = f"{done}/{self.total}"
            return f"  {label} [{bar}] {int(frac * 100):3d}%  {amount}"
        return f"  {label} {done / 1048576:.1f} MB"

    def update(self, done):
        if not IS_TTY:
            return
        now = time.monotonic()
        finished = bool(self.total) and done >= self.total
        if now - self._last < 0.05 and not finished:
            return
        self._last = now
        sys.stdout.write("\r" + self._render(done))
        sys.stdout.flush()

    def finish(self):
        if IS_TTY:
            sys.stdout.write("\r" + " " * (WIDTH - 1) + "\r")
            sys.stdout.flush()


def clean(text):
    """Strip control characters so message text can't mess with the terminal."""
    return re.sub(r"[\x00-\x1f\x7f]", "", text).strip()


def ask_yes_no(question, default=False):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {hint} ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


# ---------------------------------------------------------------------
#  Small utilities
# ---------------------------------------------------------------------

def minecraft_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / ".minecraft"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/minecraft"
    return Path.home() / ".minecraft"


def fetch(url, payload=None, label=None, headers=None) -> bytes:
    """
    GET a URL (or POST `payload` as JSON). If `label` is given, show a progress
    bar while downloading.
    """
    hdrs = {"User-Agent": MODRINTH_UA}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=30) as r:
        if not label:
            return r.read()
        total = int(r.headers.get("Content-Length") or 0) or None
        bar = ProgressBar(label, total)
        chunks, done = [], 0
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                bar.update(done)
        finally:
            bar.finish()
        return b"".join(chunks)


def fetch_json(url, payload=None, headers=None):
    return json.loads(fetch(url, payload=payload, headers=headers))


def vtuple(version):
    """'0.17.3' / 'v1.2' / '0.156.0+26.2' -> tuple of ints for comparing."""
    core = str(version).split("+")[0].split("-")[0]
    return tuple(int(x) for x in re.findall(r"\d+", core))


def is_newer(a, b):
    ta, tb = vtuple(a), vtuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return ta > tb


# ---------------------------------------------------------------------
#  Reading the configuration (Discord or built-in)
# ---------------------------------------------------------------------

def _is_no(text):
    return text.strip().lower() in ("no", "n", "false", "0", "off")


def parse_messages(contents):
    """
    Turn Discord message texts (oldest first) into
    (mc_version, mods, removals, warnings).
    Ordinary chat messages (not starting with '!') are ignored silently.
    If the same slug appears in several messages, the newest one wins - so a
    later !remove beats an earlier !required, and vice versa.
    """
    version = None
    mods = {}        # slug.lower() -> Mod
    removals = {}    # slug.lower() -> slug
    warnings = []

    for n, text in enumerate(contents, 1):
        # Tolerate people wrapping the message in ``` code fences
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
            mod = Mod(
                slug=slug,
                name=clean(fields.get("name", "")),
                note=clean(fields.get("note", "")),
                required=(command == "required"),
                default=not _is_no(fields.get("default", "yes")),
            )
            removals.pop(slug.lower(), None)
            mods.pop(slug.lower(), None)   # newest message wins, and keeps its position
            mods[slug.lower()] = mod

        elif command == "remove":
            # "!remove sodium", "!remove sodium lithium", or a "slug: sodium" line
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


def fetch_discord_messages(token, channel_id):
    """Return message texts from the channel, oldest first."""
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
    return [m.get("content", "") for m in raw]


def load_config() -> Config:
    # Local testing only: read a Discord channel live if these are set.
    token = os.environ.get("MODINSTALLER_DISCORD_TOKEN")
    channel = os.environ.get("MODINSTALLER_DISCORD_CHANNEL")
    if token and channel:
        try:
            messages = fetch_discord_messages(token, channel)
            version, mods, removals, warnings = parse_messages(messages)
            if not mods and not removals:
                raise ConfigError("no !required, !optional or !remove messages found in the channel")
            if not version:
                warnings.append(f"No !version message found; using {DEFAULT_MC_VERSION}.")
                version = DEFAULT_MC_VERSION
            return Config(version, mods, DISCORD_SOURCE_LABEL, warnings, removals)
        except ConfigError as e:
            return Config(
                DEFAULT_MC_VERSION, list(DEFAULT_MODS), "built-in list",
                [f"Could not load the Discord configuration ({e}). Using the built-in list instead."],
                list(DEFAULT_REMOVALS),
            )

    # Normal path: a plain public file kept in sync with Discord by a GitHub
    # Action (see sync_discord_config.py). No token is needed to read this.
    if CONFIG_URL:
        try:
            data = fetch_json(CONFIG_URL)
            mods = [Mod(**m) for m in data.get("mods", [])]
            removals = list(data.get("removals", []))
            version = data.get("mc_version") or DEFAULT_MC_VERSION
            if not mods and not removals:
                raise ConfigError("the published config has no mods or removals listed")
            return Config(version, mods, DISCORD_SOURCE_LABEL, [], removals)
        except Exception as e:
            return Config(
                DEFAULT_MC_VERSION, list(DEFAULT_MODS), "built-in list",
                [f"Could not load the published configuration ({e}). Using the built-in list instead."],
                list(DEFAULT_REMOVALS),
            )

    return Config(DEFAULT_MC_VERSION, list(DEFAULT_MODS), "built-in list",
                  removals=list(DEFAULT_REMOVALS))


# ---------------------------------------------------------------------
#  Fabric loader: detect, compare, install
# ---------------------------------------------------------------------

@dataclass
class FabricPlan:
    state: str          # "current" | "update" | "install" | "unsupported"
    installed: str = ""
    latest: str = ""
    detail: str = ""


def find_fabric_loaders(mc_dir: Path, mc_version: str):
    """Loader versions already installed for this Minecraft version, newest first."""
    versions = mc_dir / "versions"
    prefix, suffix = "fabric-loader-", f"-{mc_version}"
    found = []
    if versions.is_dir():
        for d in versions.iterdir():
            name = d.name
            if d.is_dir() and name.startswith(prefix) and name.endswith(suffix):
                loader = name[len(prefix):-len(suffix)]
                if re.fullmatch(r"\d+(\.\d+)*", loader) and (d / f"{name}.json").is_file():
                    found.append(loader)
    return sorted(found, key=vtuple, reverse=True)


def latest_fabric_loader(mc_version):
    """Returns (version, status). status: 'ok' | 'unsupported' | 'offline'."""
    try:
        url = f"https://meta.fabricmc.net/v2/versions/loader/{urllib.parse.quote(mc_version)}"
        entries = fetch_json(url)
    except urllib.error.HTTPError as e:
        return None, ("unsupported" if e.code in (400, 404) else "offline")
    except Exception:
        return None, "offline"
    stable = [e["loader"]["version"] for e in entries if e.get("loader", {}).get("stable")]
    if not stable:
        return None, "unsupported"
    return max(stable, key=vtuple), "ok"


def plan_fabric(mc_dir: Path, mc_version: str) -> FabricPlan:
    installed = find_fabric_loaders(mc_dir, mc_version)
    newest = installed[0] if installed else ""
    with Spinner("Checking the Fabric loader"):
        latest, status = latest_fabric_loader(mc_version)

    if status == "unsupported":
        return FabricPlan("unsupported", newest, "",
                          f"Fabric doesn't support Minecraft {mc_version} yet.")
    if status == "offline":
        if newest:
            return FabricPlan("current", newest, "",
                              f"loader {newest} is installed (couldn't check for a newer one)")
        return FabricPlan("install", "", "", "latest loader")
    if not newest:
        return FabricPlan("install", "", latest, f"loader {latest}")
    if is_newer(latest, newest):
        return FabricPlan("update", newest, latest, f"loader {newest} -> {latest}")
    return FabricPlan("current", newest, latest, f"loader {newest} is already installed")


def install_fabric(mc_dir: Path, mc_version: str, loader=None):
    """Returns (ok, detail)."""
    try:
        installers = fetch_json("https://meta.fabricmc.net/v2/versions/installer")
        url = next(i["url"] for i in installers if i["stable"])
        jar = Path(tempfile.gettempdir()) / "fabric-installer.jar"
        jar.write_bytes(fetch(url, label="Fabric installer"))
        cmd = ["java", "-jar", str(jar), "client", "-dir", str(mc_dir), "-mcversion", mc_version]
        if loader:
            cmd += ["-loader", loader]
        with Spinner("Installing the Fabric loader (this can take a moment)"):
            proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = [l for l in (proc.stderr or proc.stdout or "").splitlines() if l.strip()][-3:]
            return False, "The Fabric installer failed" + (": " + " | ".join(tail) if tail else ".")
    except FileNotFoundError:
        return False, "Java was not found. Install Java and run the installer again."
    except Exception as e:
        return False, f"Could not run the Fabric installer ({e})."

    if not find_fabric_loaders(mc_dir, mc_version):
        return True, ("The installer reported success but the Fabric version folder wasn't "
                      "found. Check your launcher.")
    return True, ""


# ---------------------------------------------------------------------
#  Mods: inspect the mods folder, compare versions, install, remove
# ---------------------------------------------------------------------

@dataclass
class LocalJar:
    path: Path
    sha512: str
    version: dict = None      # Modrinth version object, if Modrinth recognises the file


@dataclass
class ModPlan:
    mod: Mod
    action: str               # "install" | "update" | "current" | "unavailable"
    version: dict = None      # target Modrinth version
    file: dict = None         # target file within that version
    installed_version: str = ""
    installed_paths: list = field(default_factory=list)
    detail: str = ""          # reason when unavailable


@dataclass
class RemovalPlan:
    slug: str
    title: str
    paths: list = field(default_factory=list)
    error: str = ""


def sha512_file(path: Path):
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jar_mod_id(source):
    """Return the Fabric mod ID declared inside a jar (path or file-like), or None."""
    try:
        with zipfile.ZipFile(source) as z:
            return json.loads(z.read("fabric.mod.json")).get("id")
    except Exception:
        return None


def scan_local_jars(mods_dir: Path):
    """
    Hash every jar in the mods folder and ask Modrinth which projects/versions
    they are. Returns (list[LocalJar], identified_ok).
    """
    paths = sorted(mods_dir.glob("*.jar")) if mods_dir.is_dir() else []
    if not paths:
        return [], True

    bar = ProgressBar("Reading your mods folder", len(paths), unit="items")
    local = []
    try:
        for i, p in enumerate(paths, 1):
            local.append(LocalJar(p, sha512_file(p)))
            bar.update(i)
    finally:
        bar.finish()

    try:
        with Spinner("Asking Modrinth which mods you already have"):
            found = fetch_json("https://api.modrinth.com/v2/version_files",
                               payload={"hashes": [j.sha512 for j in local],
                                        "algorithm": "sha512"})
        for j in local:
            j.version = found.get(j.sha512)
        return local, True
    except Exception:
        return local, False


def pick_version(versions):
    """Newest release (falling back to beta/alpha only if there is no release)."""
    ordered = sorted(versions, key=lambda v: v.get("version_type") != "release")  # stable sort
    v = ordered[0]
    files = v["files"]
    return v, next((x for x in files if x.get("primary")), files[0])


def plan_mod(mod: Mod, mc_version: str, local) -> ModPlan:
    query = urllib.parse.urlencode({
        "game_versions": json.dumps([mc_version]),
        "loaders": json.dumps(["fabric"]),
    })
    url = f"https://api.modrinth.com/v2/project/{urllib.parse.quote(mod.slug, safe='')}/version?{query}"
    try:
        versions = fetch_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ModPlan(mod, "unavailable", detail=f"no mod named '{mod.slug}' exists on Modrinth")
        return ModPlan(mod, "unavailable", detail=f"Modrinth returned HTTP {e.code}")
    except Exception as e:
        return ModPlan(mod, "unavailable", detail=f"could not reach Modrinth ({e})")

    if not versions:
        return ModPlan(mod, "unavailable", detail=f"no Fabric build for Minecraft {mc_version} yet")

    version, f = pick_version(versions)
    project_id = version.get("project_id")
    mine = [j for j in local if j.version and j.version.get("project_id") == project_id]
    current = next((j for j in mine
                    if j.sha512 == f["hashes"]["sha512"] or j.version.get("id") == version.get("id")),
                   None)
    if current:
        return ModPlan(mod, "current", version, f,
                       installed_version=current.version.get("version_number", ""))
    if mine:
        return ModPlan(mod, "update", version, f,
                       installed_version=mine[0].version.get("version_number", "an older version"),
                       installed_paths=[j.path for j in mine])
    return ModPlan(mod, "install", version, f)


def apply_mod_plan(plan: ModPlan, mods_dir: Path, index: int, total: int):
    """Download + verify + write one mod, replacing older copies. Returns (ok, detail)."""
    f = plan.file
    filename = Path(f["filename"]).name          # never trust a path from the network
    try:
        data = fetch(f["url"], label=f"[{index}/{total}] {plan.mod.name}")
    except Exception as e:
        return False, f"download failed ({e})"
    if hashlib.sha512(data).hexdigest() != f["hashes"]["sha512"]:
        return False, "the download failed its integrity check, so it was not installed"

    (mods_dir / filename).write_bytes(data)

    # Delete older copies only after the new file is safely written
    replaced = 0
    for old in plan.installed_paths:
        if old.name != filename and old.exists():
            old.unlink()
            replaced += 1
    new_id = jar_mod_id(io.BytesIO(data))
    if new_id:   # also catches copies Modrinth didn't recognise (matched by mod ID)
        for old in mods_dir.glob("*.jar"):
            if old.name != filename and jar_mod_id(old) == new_id:
                old.unlink()
                replaced += 1

    number = plan.version.get("version_number", "")
    if plan.action == "update":
        detail = f"{plan.installed_version} -> {number}  ({filename})"
    else:
        detail = f"{number}  ({filename})"
        if replaced:
            detail += f"; replaced {replaced} older file{'s' if replaced != 1 else ''}"
    return True, detail


def plan_removal(slug: str, local, identified_ok: bool) -> RemovalPlan:
    try:
        project = fetch_json(f"https://api.modrinth.com/v2/project/{urllib.parse.quote(slug, safe='')}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return RemovalPlan(slug, slug, error=f"no mod named '{slug}' exists on Modrinth "
                                                 f"(check the slug)")
        return RemovalPlan(slug, slug, error=f"Modrinth returned HTTP {e.code}")
    except Exception as e:
        return RemovalPlan(slug, slug, error=f"could not reach Modrinth ({e})")
    title = project.get("title") or slug
    if not identified_ok:
        return RemovalPlan(slug, title, error="couldn't identify the files in your mods folder")
    paths = [j.path for j in local if j.version and j.version.get("project_id") == project.get("id")]
    return RemovalPlan(slug, title, paths)


# ---------------------------------------------------------------------
#  Self-update from GitHub releases
# ---------------------------------------------------------------------

def parse_repo(url):
    m = re.match(r"^https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), m.group(2)) if m else None


def get_latest_release(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        data = fetch_json(url, headers={"Accept": "application/vnd.github+json"})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("no releases have been published yet")
        if e.code in (403, 429):
            raise UpdateError("GitHub is rate-limiting requests right now; try again later")
        raise UpdateError(f"GitHub returned HTTP {e.code}")
    except Exception as e:
        raise UpdateError(f"could not reach GitHub ({e})")
    tag = data.get("tag_name") or ""
    if not tag:
        raise UpdateError("the latest release has no tag")
    page = data.get("html_url") or ""
    if not page.startswith("https://github.com/"):
        page = PROJECT_URL
    return {"tag": tag, "version": tag.lstrip("vV"), "url": page,
            "assets": data.get("assets") or []}


def find_release_asset(assets, base_name):
    """
    Find the release asset matching base_name (e.g. "Artisan-Installer.exe"),
    tolerating a version tag baked into the filename, since a release's asset
    is often named "Artisan-Installer-v1.2.0.exe" or "Artisan-Installer-2.0.1.exe"
    rather than the bare name. Matches "<stem>[-_ ][v]<version><suffix>",
    case-insensitively, falling back to an exact match. Returns None if
    nothing matches.
    """
    stem, suffix = Path(base_name).stem, Path(base_name).suffix
    pattern = re.compile(
        rf'^{re.escape(stem)}(?:[-_ ]v?\d[\w.\-]*)?{re.escape(suffix)}$', re.IGNORECASE
    )
    candidates = [a for a in assets if pattern.match(a.get("name") or "")]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # More than one match is unusual for a single release; prefer an exact
    # name match if there is one, otherwise just take the first.
    return next((a for a in candidates if a.get("name") == base_name), candidates[0])


def apply_update(release):
    """Download the release's script, verify it, and replace this file. Raises UpdateError."""
    owner, repo = parse_repo(PROJECT_URL)
    target = Path(__file__).resolve()

    asset = next((a for a in release["assets"] if a.get("name") == RELEASE_SCRIPT_NAME), None)
    if asset:
        url, digest = asset["browser_download_url"], asset.get("digest") or ""
    else:   # no matching asset: use the file as it was at the release tag
        url = (f"https://raw.githubusercontent.com/{owner}/{repo}/"
               f"{urllib.parse.quote(release['tag'])}/{RELEASE_SCRIPT_NAME}")
        digest = ""

    try:
        data = fetch(url, label="Downloading the update")
    except Exception as e:
        raise UpdateError(f"the download failed ({e})")
    if digest.startswith("sha256:") and hashlib.sha256(data).hexdigest() != digest[7:].lower():
        raise UpdateError("the downloaded file failed its integrity check")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UpdateError("the downloaded file isn't valid text")
    try:
        compile(text, RELEASE_SCRIPT_NAME, "exec")
    except SyntaxError as e:
        raise UpdateError(f"the downloaded script has a syntax error (line {e.lineno})")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise UpdateError("the downloaded file doesn't look like this installer")
    if not is_newer(m.group(1), VERSION):
        raise UpdateError(f"the release's script says VERSION = \"{m.group(1)}\", which isn't "
                          f"newer than yours ({VERSION}). The repo owner must bump VERSION "
                          f"before publishing a release.")

    try:
        compile(text, RELEASE_SCRIPT_NAME, "exec")
        staged = target.with_name(target.name + ".new")
        backup = target.with_name(target.name + ".bak")
        staged.write_bytes(text.encode("utf-8"))
        shutil.copy2(target, backup)
        os.replace(staged, target)
    except (OSError, SyntaxError) as e:
        raise UpdateError(f"couldn't write the update ({e})")
    return backup


WINDOWS_UPDATE_HELPER = """\
@echo off
setlocal enabledelayedexpansion
set "OLDEXE={old}"
set "NEWEXE={new}"
set "BAKEXE={backup}"
set "WAITPID={pid}"

set /a tries=0
:waitloop
tasklist /FI "PID eq %WAITPID%" 2>NUL | find /I "%WAITPID%" >NUL
if not errorlevel 1 (
    set /a tries+=1
    if !tries! GEQ 30 goto cleanup
    ping -n 2 127.0.0.1 >NUL
    goto waitloop
)

set /a tries=0
:moveloop
move /Y "%OLDEXE%" "%BAKEXE%" >NUL 2>&1
if exist "%OLDEXE%" (
    set /a tries+=1
    if !tries! GEQ 10 goto cleanup
    ping -n 2 127.0.0.1 >NUL
    goto moveloop
)

move /Y "%NEWEXE%" "%OLDEXE%" >NUL 2>&1
start "" "%OLDEXE%"

:cleanup
del "%~f0" >NUL 2>&1
"""


def write_windows_update_helper(old: Path, new: Path, backup: Path, pid: int) -> Path:
    """
    Write a small batch script that waits for this process (by PID) to exit, then
    swaps the downloaded build into place and relaunches it. Windows won't let a
    running .exe overwrite or delete itself, so the swap has to happen from an
    outside process after we've exited - this script is that outside process.
    `timeout` is avoided because it fails when stdin isn't a real console (which is
    the case for a detached helper); `ping -n 2 127.0.0.1` is the standard batch
    substitute for "sleep ~1 second".
    """
    script = WINDOWS_UPDATE_HELPER.format(old=old, new=new, backup=backup, pid=pid)
    path = Path(tempfile.gettempdir()) / f"{PROJECT_NAME}-update-{pid}.bat"
    path.write_text(script, encoding="utf-8")
    return path


def apply_update_exe(release):
    """
    Update a compiled build (frozen with e.g. PyInstaller). Returns:
      'restarting' - a detached helper is waiting for this process to exit, after
                     which it swaps in the new build and relaunches it (Windows).
      'done'       - the running executable's file was replaced immediately, since
                     Unix allows that even while it's still running (macOS/Linux).
    Raises UpdateError, with nothing changed, on any failure.
    """
    asset = next((a for a in release["assets"] if a.get("name") == RELEASE_SCRIPT_NAME), None)
    if not asset:
        raise UpdateError(f"the release has no '{RELEASE_SCRIPT_NAME}' asset to download")

    try:
        data = fetch(asset["browser_download_url"], label="Downloading the update")
    except Exception as e:
        raise UpdateError(f"the download failed ({e})")

    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        if hashlib.sha256(data).hexdigest() != digest[7:].lower():
            raise UpdateError("the downloaded file failed its integrity check")
    else:
        say("Note: this release doesn't include a checksum for the build, so it "
            "can't be verified beyond its size.", indent=2, color="yellow")
    if len(data) < 1_000_000:   # a real PyInstaller build is always much bigger than this
        raise UpdateError("the downloaded file is too small to be a real build")

    target = Path(sys.executable).resolve()

    if sys.platform == "win32":
        staged = target.with_name(f"{target.stem}.new{target.suffix}")
        backup = target.with_name(f"{target.stem}.bak{target.suffix}")
        try:
            staged.write_bytes(data)
        except OSError as e:
            raise UpdateError(f"couldn't save the update next to the program ({e})")
        try:
            helper = write_windows_update_helper(target, staged, backup, os.getpid())
            subprocess.Popen(
                ["cmd.exe", "/c", str(helper)],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError as e:
            staged.unlink(missing_ok=True)
            raise UpdateError(f"couldn't start the updater ({e})")
        return "restarting", None

    # macOS / Linux: replacing the file of a running executable works immediately;
    # the change takes effect the next time the program is started.
    staged = target.with_name(target.name + ".new")
    backup = target.with_name(target.name + ".bak")
    try:
        staged.write_bytes(data)
        staged.chmod(target.stat().st_mode)
        shutil.copy2(target, backup)
        os.replace(staged, target)
    except OSError as e:
        raise UpdateError(f"couldn't replace the program file ({e})")
    return "done", backup


def update_flow(assume_yes=False, explicit=False):
    """
    Returns 'updated' | 'current' | 'declined' | 'failed' | 'unavailable'.
    explicit=True (--update) reports problems; the automatic check stays quiet
    unless there is actually a newer release.
    """
    repo = parse_repo(PROJECT_URL)
    if not repo:
        if explicit:
            say("PROJECT_URL isn't a GitHub repository, so updates aren't available.", color="red")
        return "unavailable"

    release, error = None, None
    with Spinner("Checking for a newer version of this installer"):
        try:
            release = get_latest_release(*repo)
        except UpdateError as e:
            error = str(e)
    if error:
        if explicit:
            say(f"Couldn't check for updates: {error}.", color="red")
        return "unavailable"

    if not is_newer(release["version"], VERSION):
        say(f"This installer is up to date (v{VERSION}).", color="green" if explicit else "dim")
        return "current"

    say(f"A newer version of this installer is available: v{release['version']} "
        f"(you have v{VERSION}).", color="yellow", bold=True)
    say(f"Release page: {release['url']}", indent=2, color="dim")

    if not assume_yes and not ask_yes_no("Update now?", default=True):
        say("Okay, continuing with the current version.", color="dim")
        return "declined"

    if getattr(sys, "frozen", False):
        try:
            outcome, backup = apply_update_exe(release)
        except UpdateError as e:
            say(f"Update failed: {e}", color="red")
            say("Your current copy was not changed. You can also grab it yourself:", color="dim")
            say(release["url"], indent=2, color="dim")
            return "failed"
        if outcome == "restarting":
            say(f"Downloaded v{release['version']}. Restarting to finish the update...",
                color="green", bold=True)
            global _SKIP_EXIT_PROMPT
            _SKIP_EXIT_PROMPT = True
            return "restarting"
        say(f"Updated to v{release['version']}. The old version was saved as {backup.name}.",
            color="green", bold=True)
        say("Please run the installer again to use the new version.")
        return "updated"

    try:
        backup = apply_update(release)
    except UpdateError as e:
        say(f"Update failed: {e}", color="red")
        say("Your current copy was not changed.", color="dim")
        return "failed"
    say(f"Updated to v{release['version']}. The old version was saved as {backup.name}.",
        color="green", bold=True)
    say("Please run the installer again to use the new version.")
    return "updated"


# ---------------------------------------------------------------------
#  Screens
# ---------------------------------------------------------------------

def print_title():
    rule("Fabric + Mods Installer")


def print_intro(cfg: Config, mods_dir: Path):
    required = [m for m in cfg.mods if m.required]
    optional = [m for m in cfg.mods if not m.required]

    say(f"Minecraft version: {cfg.mc_version}", bold=True)
    say(f"Mod list loaded from: {cfg.source}")
    for w in cfg.warnings:
        say(f"! {w}", indent=2, color="yellow")
    print()
    say("This program will:", bold=True)
    print()
    step = 1
    say(f"{step}. Check for the Fabric loader for Minecraft {cfg.mc_version} and install "
        f"or update it only if needed (this adds a Fabric profile to your launcher).",
        indent=2, hang=3, color="white")
    if required:
        print()
        step += 1
        say(f"{step}. Install or update these REQUIRED mods in {mods_dir} "
            f"(mods that are already up to date are left alone):",
            indent=2, hang=3, color="white")
        for m in required:
            say(f"- {m.name}", indent=7, color="blue")
            if m.note:
                say(m.note, indent=9, color="dim")
    if optional:
        print()
        step += 1
        say(f"{step}. Ask you about these OPTIONAL mods (nothing is installed until you've "
            f"answered for all of them):", indent=2, hang=3, color="white")
        for m in optional:
            say(f"- {m.name}", indent=7, color="orange")
            if m.note:
                say(m.note, indent=9, color="dim")
    if cfg.removals:
        print()
        step += 1
        say(f"{step}. Remove these mods that are no longer part of the pack (only if they're "
            f"found in your mods folder):", indent=2, hang=3, color="white")
        for slug in cfg.removals:
            say(f"- {slug}", indent=7, color="white")
    print()
    say("Mods are downloaded from Modrinth. When a newer version of a mod is available, "
        "the old file is replaced. Nothing else on your computer is changed.", color="dim")
    print()


def print_plan(cfg, fplan, plans, rplans):
    print()
    say("Here's what will happen:", bold=True)
    fname = f"Fabric loader for Minecraft {cfg.mc_version}"
    labels = {
        "current": ("UP TO DATE", "green"),
        "update": ("UPDATE", "yellow"),
        "install": ("INSTALL", "green"),
        "unsupported": ("UNAVAILABLE", "red"),
    }
    lab, col = labels[fplan.state]
    status_line(lab, col, f"{fname}: {fplan.detail}" if fplan.detail else fname)

    for p in plans:
        if p.action == "current":
            status_line("UP TO DATE", "green", f"{p.mod.name}: {p.installed_version} is already installed")
        elif p.action == "update":
            status_line("UPDATE", "yellow",
                        f"{p.mod.name}: {p.installed_version} -> {p.version.get('version_number', '')}")
        elif p.action == "install":
            status_line("INSTALL", "green", f"{p.mod.name}: {p.version.get('version_number', '')}")
        else:
            status_line("UNAVAILABLE", "red", f"{p.mod.name}: {p.detail}")

    for r in rplans:
        if r.error:
            status_line("UNAVAILABLE", "red", f"{r.title}: {r.error}")
        elif r.paths:
            status_line("REMOVE", "yellow", f"{r.title}: {', '.join(p.name for p in r.paths)}")
        else:
            status_line("NOT FOUND", "dim", f"{r.title}: not in your mods folder, nothing to remove")

    if (fplan.state == "current"
            and all(p.action in ("current", "unavailable") for p in plans)
            and not any(r.paths for r in rplans)):
        print()
        say("Everything that can be installed is already up to date. Nothing to download.",
            color="green")


def print_summary(cfg, results):
    print()
    rule("SUMMARY - what happened")
    order = [("loader", None), ("required", "Required mods"),
             ("optional", "Optional mods"), ("removal", "Removed mods")]
    for group, title in order:
        rows = [r for r in results if r.group == group]
        if not rows:
            continue
        if title:
            print()
            print(paint(title + ":", bold=True))
        for r in rows:
            label, color = STATUS[r.status]
            status_line(label, color, r.name, indent=0 if group == "loader" else 2,
                        detail=r.detail)

    print()
    required_bad = any(r.group in ("loader", "required")
                       and r.status not in ("installed", "updated", "up to date")
                       for r in results)
    other_bad = any(r.group in ("optional", "removal") and r.status == "failed"
                    for r in results)
    if required_bad:
        say("Setup is INCOMPLETE: something required did not install. See the "
            "details above.", color="red", bold=True)
    else:
        if other_bad:
            say("Setup finished, but some optional installs or removals didn't work "
                "(see above).", color="orange", bold=True)
        else:
            say("All done!", color="green", bold=True)
        say("Open the Minecraft launcher and choose the Fabric profile to play.")
    return required_bad


def print_check(cfg: Config):
    rule("CONFIG CHECK (nothing will be installed)")
    say(f"Source: {cfg.source}")
    say(f"Minecraft version: {cfg.mc_version}")
    for label, group in (("REQUIRED", [m for m in cfg.mods if m.required]),
                         ("OPTIONAL", [m for m in cfg.mods if not m.required])):
        print(f"\n{paint(f'{label} mods ({len(group)}):', bold=True)}")
        for m in group:
            extra = "" if m.required else f"  [default: {'yes' if m.default else 'no'}]"
            say(f"- {m.name}  (slug: {m.slug}){extra}", indent=2)
            say(m.note or "(no note)", indent=6, color="dim")
    print(f"\n{paint(f'REMOVE ({len(cfg.removals)}):', bold=True)}")
    for slug in cfg.removals:
        say(f"- {slug}", indent=2, color="yellow")
    print()
    if cfg.warnings:
        say("Problems found:", color="yellow", bold=True)
        for w in cfg.warnings:
            say(f"! {w}", indent=2, color="yellow")
    else:
        say("No problems found.", color="green")


HELP = __doc__.split("Command-line options")[1].split("Discord message format")[0]


# ---------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------

def run(argv) -> int:
    init_terminal(no_color="--no-color" in argv)

    if "--help" in argv or "-h" in argv:
        print("Command-line options" + HELP.rstrip())
        return 0
    if "--update" in argv:
        print_title()
        return 1 if update_flow(assume_yes=True, explicit=True) == "failed" else 0
    if "--check" in argv:
        print_check(load_config())
        return 0

    print_title()
    if CHECK_FOR_UPDATES and "--no-update-check" not in argv:
        if update_flow() in ("updated", "restarting"):
            return 0
        print()

    mc = minecraft_dir()
    if not mc.exists():
        say(f"Minecraft folder not found at {mc}. Run the game once first, then try again.",
            color="red")
        return 1
    mods_dir = mc / "mods"

    with Spinner("Loading the mod list"):
        cfg = load_config()
    print_intro(cfg, mods_dir)

    install_all = "--all" in argv
    if install_all and any(not m.required for m in cfg.mods):
        say("The --all flag was used: every optional mod will be installed "
            "automatically, without asking.", color="orange", bold=True)
        print()

    if not ask_yes_no(paint("Continue with the installation?", bold=True), default=True):
        say("Cancelled. Nothing was changed.", color="yellow")
        return 0

    # ---- Questions first: nothing is downloaded until every answer is in ----
    optional = [m for m in cfg.mods if not m.required]
    chosen = {}
    if install_all:
        chosen = {m.slug: True for m in optional}
    else:
        for i, m in enumerate(optional, 1):
            print()
            say(f"Optional mod {i} of {len(optional)}: {m.name}", bold=True)
            if m.note:
                say(m.note, indent=2, color="dim")
            say("Nothing is installed yet: downloads start after you've answered for every "
                "optional mod.", indent=2, color="orange")
            chosen[m.slug] = ask_yes_no(f"Install {m.name}?", default=m.default)
    if optional:
        print()
        say("Your choices:", bold=True)
        for m in optional:
            if chosen[m.slug]:
                say(f"+ {m.name}", indent=2, color="green")
            else:
                say(f"- {m.name} (skipped)", indent=2, color="yellow")

    # ---- Check what's already there ----
    print()
    say("Checking your setup...", bold=True)
    mods_dir.mkdir(parents=True, exist_ok=True)
    fplan = plan_fabric(mc, cfg.mc_version)
    local, identified_ok = scan_local_jars(mods_dir)
    if not identified_ok:
        say("! Couldn't ask Modrinth about your existing mods, so their versions can't "
            "be compared.", indent=2, color="yellow")
    wanted = [m for m in cfg.mods if m.required or chosen.get(m.slug)]
    plans = []
    for i, m in enumerate(wanted, 1):
        with Spinner(f"Checking {m.name} ({i}/{len(wanted)})"):
            plans.append(plan_mod(m, cfg.mc_version, local))
    rplans = []
    for slug in cfg.removals:
        with Spinner(f"Looking for {slug} in your mods folder"):
            rplans.append(plan_removal(slug, local, identified_ok))
    print_plan(cfg, fplan, plans, rplans)

    # ---- Apply ----
    print()
    say("Applying changes...", bold=True)
    results = []

    fname = f"Fabric loader for Minecraft {cfg.mc_version}"
    if fplan.state == "current":
        fabric_ok = True
        results.append(Result(fname, "loader", "up to date", fplan.detail))
    elif fplan.state == "unsupported":
        fabric_ok = False
        results.append(Result(fname, "loader", "failed", fplan.detail))
    else:
        ok, detail = install_fabric(mc, cfg.mc_version, fplan.latest or None)
        fabric_ok = ok
        status = ("updated" if fplan.state == "update" else "installed") if ok else "failed"
        info = detail or fplan.detail
        results.append(Result(fname, "loader", status, info))
        say(f"{'OK' if ok else 'FAILED'}: {fname}", indent=2, color="green" if ok else "red")

    by_slug = {}
    todo = [p for p in plans if p.action in ("install", "update")]
    n = 0
    for p in plans:
        m = p.mod
        group = "required" if m.required else "optional"
        if p.action == "current":
            by_slug[m.slug] = Result(m.name, group, "up to date",
                                     f"{p.installed_version} is already installed")
        elif p.action == "unavailable":
            by_slug[m.slug] = Result(m.name, group, "failed", p.detail)
        elif not fabric_ok:
            by_slug[m.slug] = Result(m.name, group, "not run",
                                     "Skipped because the Fabric install failed.")
        else:
            n += 1
            ok, detail = apply_mod_plan(p, mods_dir, n, len(todo))
            status = ("updated" if p.action == "update" else "installed") if ok else "failed"
            by_slug[m.slug] = Result(m.name, group, status, detail)
            say(f"{'OK' if ok else 'FAILED'}: {m.name}", indent=2, color="green" if ok else "red")
    for m in cfg.mods:
        if m.slug in by_slug:
            results.append(by_slug[m.slug])
        else:
            results.append(Result(m.name, "optional", "skipped",
                                  "You chose not to install this one."))

    for r in rplans:
        if r.error:
            results.append(Result(r.title, "removal", "failed", r.error))
        elif not r.paths:
            results.append(Result(r.title, "removal", "not found",
                                  "Not in your mods folder (only files downloaded from "
                                  "Modrinth can be matched)."))
        else:
            try:
                for path in r.paths:
                    path.unlink()
                results.append(Result(r.title, "removal", "removed",
                                      ", ".join(p.name for p in r.paths)))
                say(f"REMOVED: {r.title}", indent=2, color="yellow")
            except OSError as e:
                results.append(Result(r.title, "removal", "failed", f"couldn't delete it ({e})"))

    return 1 if print_summary(cfg, results) else 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # never crash on odd characters
        except Exception:
            pass
    try:
        code = run(sys.argv[1:])
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    if not ({"--check", "--help", "-h"} & set(sys.argv)) and not _SKIP_EXIT_PROMPT:
        try:
            input("\nPress Enter to exit...")      # keeps the window open as an .exe
        except EOFError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
