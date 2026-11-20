"""Update check (krosta app-standard UPDATES.md).

Asks the GitHub Releases of the repo at most once a day and sends nothing else.
Turn it off with UPDATE_CHECK=0 in the environment.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "krosta-codes/dj-library-sync"
HOME = Path(__file__).resolve().parent.parent
CACHE = Path.home() / ".cache" / "djsync" / "update-check.json"
DAY = 86_400


def current_version() -> str:
    try:
        return (HOME / "VERSION").read_text().strip()
    except OSError:
        return "0"


def is_newer(a: str, b: str) -> bool:
    def parts(s: str) -> list[int]:
        out = []
        for p in s.lstrip("vV").split("."):
            digits = "".join(ch for ch in p if ch.isdigit()) or "0"
            out.append(int(digits))
        return out
    x, y = parts(a), parts(b)
    n = max(len(x), len(y))
    x += [0] * (n - len(x))
    y += [0] * (n - len(y))
    return x > y


def fetch_latest() -> str | None:
    """Tag of the latest release, or None. Falls back to `gh` while the repo is private."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"djsync/{current_version()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("tag_name")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None
    except (OSError, ValueError):
        return None
    gh = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None)
    if not gh:
        return None
    try:
        r = subprocess.run([gh, "api", f"repos/{REPO}/releases/latest", "-q", ".tag_name"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(state))
    except OSError:
        pass


def daily_check() -> str | None:
    """Newer tag if one is out, checked at most once per day. Never raises."""
    if os.environ.get("UPDATE_CHECK", "1") == "0":
        return None
    state = _load()
    if time.time() - state.get("checked", 0) >= DAY:
        state = {**state, "checked": time.time(), "latest": fetch_latest() or state.get("latest")}
        _save(state)
    latest = state.get("latest")
    return latest if latest and is_newer(latest, current_version()) else None


def already_notified(tag: str) -> bool:
    """True if this tag was announced before; marks it as announced."""
    state = _load()
    if state.get("notified") == tag:
        return True
    _save({**state, "notified": tag})
    return False


def run_update() -> int:
    """`djsync update`: pull the clone and refresh dependencies."""
    before = current_version()
    latest = fetch_latest()
    if latest and not is_newer(latest, before):
        print(f"djsync {before} is the latest version.")
        return 0
    if not (HOME / ".git").exists():
        print(f"Not a git clone. Download the new version: https://github.com/{REPO}/releases")
        return 1
    steps = [
        ["git", "-C", str(HOME), "pull", "--ff-only", "--quiet"],
        [str(HOME / ".venv" / "bin" / "pip"), "install", "--quiet", "--upgrade", "pyrekordbox"],
    ]
    for cmd in steps:
        if subprocess.run(cmd).returncode != 0:
            print("Update failed at: " + " ".join(cmd), file=sys.stderr)
            return 1
    _save({**_load(), "checked": 0})
    print(f"djsync updated: {before} -> {current_version()}")
    return 0
