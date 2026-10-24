"""Configuration: paths, mappings, tunables."""
from __future__ import annotations

import os
import plistlib
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


def boot_volume_name() -> str:
    """Traktor stores the volume label, not '/'. Read it from diskutil."""
    try:
        out = subprocess.run(
            ["diskutil", "info", "-plist", "/"], capture_output=True, timeout=10
        ).stdout
        return plistlib.loads(out).get("VolumeName", "Macintosh HD")
    except Exception:
        return "Macintosh HD"


def find_traktor_collection() -> Path | None:
    """Newest 'Traktor <version>/collection.nml' under ~/Documents/Native Instruments."""
    base = HOME / "Documents" / "Native Instruments"
    if not base.is_dir():
        return None

    def version_key(p: Path):
        digits = [int(x) for x in p.parent.name.split()[-1].split(".") if x.isdigit()]
        return digits or [0]

    found = sorted(base.glob("Traktor */collection.nml"), key=version_key)
    return found[-1] if found else None


@dataclass
class Config:
    rekordbox_db: Path = HOME / "Library" / "Pioneer" / "rekordbox" / "master.db"
    traktor_nml: Path | None = None
    volume: str = field(default_factory=boot_volume_name)

    # Behaviour
    sync_cues: bool = True
    sync_beatgrid: bool = True
    sync_ratings: bool = True
    sync_playlists: bool = True
    # Fold rekordbox colour + My Tags into the Traktor comment field
    tags_into_comment: bool = True
    # Set Traktor's Analysis Lock on tracks Traktor has already analysed, so it
    # can never overwrite the imported grid, BPM, key or gain.
    analysis_lock: bool = False
    # Move a cue in rekordbox slot I or beyond into the first free Traktor slot
    # instead of dropping it. Only ever fills an empty pad, never moves a cue
    # that already has one.
    remap_overflow_cues: bool = False
    # rekordbox playlist name -> Traktor node name, for renames a number prefix
    # cannot explain (for example "Drummm Bass" -> "4 DnB").
    playlist_renames: dict = field(default_factory=dict)
    # Remove Traktor entries whose file no longer exists on disk
    remove_dead_files: bool = False
    # rekordbox playlists to skip (exact names, case-insensitive)
    skip_playlists: list[str] = field(default_factory=lambda: ["History"])
    # Skip playlists whose name starts with any of these
    skip_playlist_prefixes: list[str] = field(default_factory=lambda: ["HISTORY "])
    # Only sync tracks under these roots ([] = all)
    include_roots: list[str] = field(default_factory=list)

    backups: int = 20
    backup_dir: Path = ROOT / "backups"
    state_file: Path = ROOT / "state.json"
    log_file: Path = ROOT / "logs" / "djsync.log"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        cfg = cls()
        cfg.traktor_nml = find_traktor_collection()
        path = path or DEFAULT_CONFIG
        if path.is_file():
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            cfg.playlist_renames = dict(data.get("playlists", {}).get("rename", {}))
            for key, value in {**data.get("paths", {}), **data.get("sync", {})}.items():
                if not hasattr(cfg, key):
                    continue
                current = getattr(cfg, key)
                setattr(cfg, key, Path(os.path.expanduser(value))
                        if isinstance(current, Path) or (current is None and key.endswith(("_db", "_nml", "_dir", "_file")))
                        else value)
        return cfg
