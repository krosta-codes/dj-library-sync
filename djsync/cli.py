"""djsync: one-way rekordbox 6 to Traktor 4 library sync."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import Config
from .rekordbox import RekordboxSnapshot
from .traktor import SyncReport, TraktorCollection

log = logging.getLogger("djsync")


def _running(pattern: str) -> bool:
    """True if a process matching `pattern` is running (excluding this script)."""
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return bool(result.stdout.strip())


def traktor_running() -> bool:
    return _running(r"/Traktor\.app/Contents/MacOS/Traktor")


def rekordbox_running() -> bool:
    return _running(r"/rekordbox\.app/Contents/MacOS/rekordbox")


def notify(title: str, message: str) -> None:
    """macOS notification, so a background sync is not invisible."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def setup_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(stream)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(fmt)
    log.addHandler(handler)
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)


def fingerprint(cfg: Config) -> str:
    """Cheap change detector: size and mtime of the rekordbox database files."""
    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        path = Path(str(cfg.rekordbox_db) + suffix)
        if path.exists():
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def run_sync(args: argparse.Namespace, cfg: Config) -> int:
    if cfg.traktor_nml is None:
        log.error("No Traktor collection.nml found under ~/Documents/Native Instruments")
        return 2
    if not cfg.rekordbox_db.exists():
        log.error("rekordbox database not found at %s", cfg.rekordbox_db)
        return 2

    if traktor_running() and not args.force:
        # Traktor holds the collection in memory and rewrites it on quit, so a
        # write now would be silently thrown away.
        log.info("Traktor is running, skipping. Quit Traktor and sync again.")
        return 0
    if rekordbox_running() and not args.force:
        log.info("rekordbox is running, skipping so we never read a half-written database.")
        return 0

    state = load_state(cfg.state_file)
    current = fingerprint(cfg)
    if not args.dry_run and not args.force and state.get("fingerprint") == current:
        log.info("rekordbox library unchanged since %s, nothing to do.",
                 state.get("last_sync", "last run"))
        return 0

    report = SyncReport()
    log.info("Reading rekordbox library ...")
    with RekordboxSnapshot(cfg.rekordbox_db) as snapshot:
        tracks = snapshot.tracks(cfg.include_roots)
        playlists = snapshot.playlists(cfg.skip_playlists, cfg.skip_playlist_prefixes) \
            if cfg.sync_playlists else []
    log.info("  %d tracks, %d root playlist nodes", len(tracks), len(playlists))

    collection = TraktorCollection(cfg.traktor_nml, cfg.volume)
    log.info("Traktor collection has %d entries before sync",
             len(collection.collection.findall("ENTRY")))

    collection.merge_tracks(tracks, cfg, report)
    if cfg.sync_playlists:
        collection.rebuild_playlists(playlists, report, cfg.playlist_renames)

    log.info("Result: %s", report.summary())
    if report.playlist_tracks_missing:
        log.info("  %d playlist entries skipped (track not in the Traktor collection)",
                 report.playlist_tracks_missing)

    if args.dry_run:
        log.info("Dry run, nothing written to %s", cfg.traktor_nml)
        return 0

    backup = collection.save(cfg.backup_dir, cfg.backups)
    if backup:
        log.info("Backup: %s", backup)
    log.info("Wrote %s", cfg.traktor_nml)
    if args.notify and (report.added or report.updated or report.removed):
        notify("rekordbox to Traktor",
               f"{report.added} added, {report.updated} updated, {report.playlists} playlists")

    cfg.state_file.write_text(json.dumps({
        "fingerprint": current,
        "last_sync": datetime.now().isoformat(timespec="seconds"),
        "tracks": len(tracks),
        "report": report.__dict__,
    }, indent=2))
    return 0


def run_status(cfg: Config) -> int:
    state = load_state(cfg.state_file)
    print(f"rekordbox db   : {cfg.rekordbox_db} "
          f"({'found' if cfg.rekordbox_db.exists() else 'MISSING'})")
    print(f"Traktor nml    : {cfg.traktor_nml or 'NOT FOUND'}")
    print(f"Boot volume    : {cfg.volume}")
    print(f"rekordbox open : {rekordbox_running()}")
    print(f"Traktor open   : {traktor_running()}")
    print(f"Last sync      : {state.get('last_sync', 'never')}")
    if state.get("report"):
        print(f"Last result    : {state['report']}")
    pending = state.get("fingerprint") != fingerprint(cfg)
    print(f"Changes pending: {pending}")
    backups = sorted(cfg.backup_dir.glob("collection-*.nml"))
    print(f"Backups        : {len(backups)}"
          + (f" (newest {backups[-1].name})" if backups else ""))
    return 0


def run_restore(args: argparse.Namespace, cfg: Config) -> int:
    backups = sorted(cfg.backup_dir.glob("collection-*.nml"))
    if not backups:
        print("No backups available.")
        return 1
    if traktor_running():
        print("Traktor is running. Quit it first, or the restore is discarded.")
        return 1
    source = Path(args.file) if args.file else backups[-1]
    if not source.exists():
        print(f"No such backup: {source}")
        return 1
    import shutil
    shutil.copy2(source, cfg.traktor_nml)
    print(f"Restored {source.name} to {cfg.traktor_nml}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="djsync", description="Sync the rekordbox library and playlists into Traktor 4.")
    parser.add_argument("command", nargs="?", default="sync",
                        choices=["sync", "status", "restore"])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="sync even if unchanged or an app is open (unsafe with Traktor open)")
    parser.add_argument("--file", help="restore: a specific backup file")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--notify", action="store_true",
                        help="post a macOS notification when something changed")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    setup_logging(cfg.log_file, args.verbose)

    if args.command == "status":
        return run_status(cfg)
    if args.command == "restore":
        return run_restore(args, cfg)
    try:
        return run_sync(args, cfg)
    except Exception:
        log.exception("Sync failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
