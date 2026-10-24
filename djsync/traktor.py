"""Read, merge and write a Traktor collection.nml."""
from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .rekordbox import Playlist, Track

log = logging.getLogger("djsync")

# Traktor MUSICAL_KEY: 0..11 major C..B, 12..23 minor Cm..Bm
_PITCH = {"C": 0, "Db": 1, "D": 2, "Eb": 3, "E": 4, "F": 5,
          "Gb": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}
_ENHARMONIC = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb",
               "Cb": "B", "Fb": "E", "E#": "F", "B#": "C"}
_CAMELOT = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "Gbm", "12A": "Dbm",
    "1B": "B", "2B": "Gb", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}


_UUID_NS = uuid.UUID("6f9b1e8a-0d3c-4f5b-9a21-b7c4de000001")


# "1 Techno", "01. Techno", "2 - House" all match the rekordbox playlist "Techno".
_NUM_PREFIX = re.compile(r"^\s*\d+\s*[.)\-_]?\s+(?P<rest>.+)$")
_MATCHABLE = ("FOLDER", "PLAYLIST")


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def match_node(children: list, rb_name: str, renames: dict) -> tuple:
    """Find the Traktor node standing in for a rekordbox playlist.

    Traktor node names drift from rekordbox because they get renamed for sort
    order. Match on the explicit rename map first, then the exact name, then
    the name with a leading sort number stripped.
    """
    candidates = [(i, n) for i, n in enumerate(children) if n.get("TYPE") in _MATCHABLE]
    wanted = renames.get(rb_name)
    if wanted:
        for i, node in candidates:
            if node.get("NAME") == wanted:
                return i, node
    for i, node in candidates:
        if node.get("NAME") == rb_name:
            return i, node
    target = _norm(rb_name)
    for i, node in candidates:
        name = node.get("NAME") or ""
        if _norm(name) == target:
            return i, node
        stripped = _NUM_PREFIX.match(name)
        if stripped and _norm(stripped.group("rest")) == target:
            return i, node
    return None, None


def playlist_uuid(path_prefix: str, name: str) -> str:
    """Stable per-playlist UUID, so an unchanged tree serialises identically."""
    return uuid.uuid5(_UUID_NS, f"{path_prefix}/{name}").hex


def musical_key_value(key: str) -> int | None:
    """rekordbox key text ('8A', 'Am', 'F#m', 'C') to Traktor's 0..23 index."""
    if not key:
        return None
    key = key.strip()
    key = _CAMELOT.get(key.upper(), key)
    minor = key.endswith("m")
    root = key[:-1] if minor else key
    root = root.strip()
    root = _ENHARMONIC.get(root, root)
    if root not in _PITCH:
        return None
    return _PITCH[root] + (12 if minor else 0)


def encode_segment(segment: str) -> str:
    """Traktor separates path segments with '/:', and writes a literal colon in
    a name as '//'. macOS shows such a name with a slash, for example the file
    "Human Safari 9:09 AM.aiff" appears in Finder as "9/09 AM"."""
    return segment.replace(":", "//")


def decode_segment(segment: str) -> str:
    return segment.replace("//", ":")


def split_location(path: Path, boot_volume: str) -> tuple[str, str, str]:
    """Absolute POSIX path to Traktor's (volume, DIR, FILE) triple."""
    parts = path.parts
    if len(parts) > 2 and parts[1] == "Volumes":
        volume, rest = parts[2], parts[3:]
    else:
        volume, rest = boot_volume, parts[1:]
    directory = "".join(f"/:{encode_segment(segment)}" for segment in rest[:-1]) + "/:"
    return volume, directory, encode_segment(rest[-1])


def primary_key(path: Path, boot_volume: str) -> str:
    volume, directory, filename = split_location(path, boot_volume)
    return f"{volume}{directory}{filename}"


def signature(element: ET.Element) -> tuple:
    """Structural fingerprint of an element, ignoring text and whitespace.

    Comparing ET.tostring() output would flag every entry as changed, because
    rebuilding the CUE_V2 children alters the serialised whitespace.
    """
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        tuple(signature(child) for child in element),
    )


def entry_path(entry: ET.Element) -> str | None:
    """Absolute POSIX path of an existing ENTRY, for matching against rekordbox."""
    loc = entry.find("LOCATION")
    if loc is None:
        return None
    # Strip the '/:' separators first, so a '//' left behind is a real colon.
    directory = decode_segment((loc.get("DIR") or "").replace("/:", "/"))
    filename = decode_segment(loc.get("FILE") or "")
    volume = loc.get("VOLUME") or ""
    if not directory.startswith("/"):
        directory = "/" + directory
    if volume and not directory.startswith("/Volumes/") and volume != "Macintosh HD":
        return f"/Volumes/{volume}{directory}{filename}".lower()
    return f"{directory}{filename}".lower()


# Traktor INFO FLAGS bits, confirmed against a locked selection in Traktor 4.4.1.
FLAG_ANALYSIS_LOCK = 16

# Traktor 4 has eight hot cue pads, rekordbox slot I and beyond have no home.
TRAKTOR_HOTCUE_SLOTS = 8

# Traktor rewrites GRID BPM and TEMPO BPM as 32 bit floats, so 139.980000 comes
# back as 139.979996 after every session. Without a tolerance every sync would
# rewrite those entries for a difference of 4e-6 BPM.
BPM_TOLERANCE = 0.001


def bpm_equal(current: str | None, wanted: float) -> bool:
    """True when a stored BPM string is the same value as far as Traktor cares."""
    if current is None:
        return False
    try:
        return abs(float(current) - float(wanted)) < BPM_TOLERANCE
    except (TypeError, ValueError):
        return False


def _cue_attrs(element: ET.Element) -> tuple:
    """Attributes of a cue element, with BPM rounded past the float32 noise."""
    items = []
    for key, value in sorted(element.attrib.items()):
        if key == "BPM":
            try:
                value = f"{float(value):.3f}"
            except (TypeError, ValueError):
                pass
        items.append((key, value))
    return tuple(items)


def cue_signature(cues: list) -> list:
    """Order-insensitive fingerprint of a set of CUE_V2 elements."""
    return sorted(
        (_cue_attrs(cue), tuple(_cue_attrs(child) for child in cue))
        for cue in cues
    )


def is_analysed(entry: ET.Element) -> bool:
    """Traktor has done its own pass: waveform, loudness and gain exist."""
    return entry.get("AUDIO_ID") is not None


def set_analysis_lock(entry: ET.Element) -> bool:
    """Lock a track the way the Traktor UI does.

    Traktor writes the Analysis Lock twice: as INFO FLAGS bit 16 and as
    LOCK="1" plus LOCK_MODIFICATION_TIME on the ENTRY itself. Writing only the
    flag is not enough, entries locked in the UI always carry both.

    Only ever called for analysed entries: locking a track before Traktor has
    scanned it would stop the scan happening at all, so it would never get a
    waveform.
    """
    info = entry.find("INFO")
    if info is None:
        return False
    changed = False
    current = int(info.get("FLAGS") or 0)
    if not current & FLAG_ANALYSIS_LOCK:
        info.set("FLAGS", str(current | FLAG_ANALYSIS_LOCK))
        changed = True
    if entry.get("LOCK") != "1":
        entry.set("LOCK", "1")
        entry.set("LOCK_MODIFICATION_TIME",
                  datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        changed = True
    return changed


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing_files: int = 0
    removed: int = 0
    playlists: int = 0
    playlist_tracks_missing: int = 0
    overflow_cues: int = 0
    remapped_cues: int = 0
    locked: int = 0

    def summary(self) -> str:
        return (
            f"{self.added} added, {self.updated} updated, {self.unchanged} unchanged, "
            f"{self.playlists} playlists, {self.missing_files} files missing on disk, "
            f"{self.removed} removed, {self.locked} newly locked, "
            f"{self.remapped_cues} cues remapped into a free slot, "
            f"{self.overflow_cues} cues beyond Traktor's 8 slots"
        )


class TraktorCollection:
    def __init__(self, path: Path, boot_volume: str):
        self.path = Path(path)
        self.volume = boot_volume
        if self.path.exists():
            self.tree = ET.parse(self.path)
            self.root = self.tree.getroot()
        else:
            self.root = ET.Element("NML", VERSION="20")
            ET.SubElement(self.root, "HEAD",
                          COMPANY="www.native-instruments.com", PROGRAM="Traktor")
            ET.SubElement(self.root, "COLLECTION", ENTRIES="0")
            playlists = ET.SubElement(self.root, "PLAYLISTS")
            node = ET.SubElement(playlists, "NODE", TYPE="FOLDER", NAME="$ROOT")
            ET.SubElement(node, "SUBNODES", COUNT="0")
            self.tree = ET.ElementTree(self.root)
        self.collection = self.root.find("COLLECTION")
        if self.collection is None:
            self.collection = ET.SubElement(self.root, "COLLECTION", ENTRIES="0")

    # ----------------------------------------------------------------- entry

    def index(self) -> dict[str, ET.Element]:
        found: dict[str, ET.Element] = {}
        for entry in self.collection.findall("ENTRY"):
            key = entry_path(entry)
            if key:
                found[key] = entry
        return found

    # Traktor measures these from the file itself and is more precise than
    # rekordbox's integer seconds, so only fill them in when they are missing.
    FILL_ONLY_IF_ABSENT = ("PLAYTIME", "PLAYTIME_FLOAT")

    def _info_attrs(self, track: Track, sync_ratings: bool, tags_into_comment: bool) -> dict:
        comment = track.comment
        if tags_into_comment:
            extra = list(track.mytags)
            if track.color:
                extra.append(track.color)
            if extra:
                tagline = " ".join(f"#{t.replace(' ', '_')}" for t in extra)
                comment = f"{comment} {tagline}".strip()

        attrs = {
            "GENRE": track.genre,
            "LABEL": track.label,
            "REMIXER": track.remixer,
            "COMMENT": comment,
            "KEY": track.key,
            "PLAYCOUNT": str(track.playcount) if track.playcount else "",
            "PLAYTIME": str(int(round(track.length_s))) if track.length_s else "",
            "PLAYTIME_FLOAT": f"{track.length_s:.6f}" if track.length_s else "",
            "BITRATE": str(track.bitrate) if track.bitrate else "",
            "FILESIZE": str(track.filesize_kb) if track.filesize_kb else "",
            "RELEASE_DATE": track.release_date,
            "IMPORT_DATE": track.import_date,
        }
        if sync_ratings:
            # rekordbox stores 0..5 stars, Traktor 0..255 in steps of 51.
            attrs["RANKING"] = str(track.rating * 51) if track.rating else "0"
        return {k: v for k, v in attrs.items() if v != ""}

    def _cue_elements(self, track: Track, report: SyncReport,
                      remap_overflow: bool = False) -> list[ET.Element]:
        elements: list[ET.Element] = []
        if track.grid_start_ms is not None and track.grid_bpm:
            grid = ET.Element("CUE_V2", NAME="Beatgrid", DISPL_ORDER="0", TYPE="4",
                              START=f"{track.grid_start_ms:.6f}", LEN="0.000000",
                              REPEATS="-1", HOTCUE="-1")
            ET.SubElement(grid, "GRID", BPM=f"{track.grid_bpm:.6f}")
            elements.append(grid)
        # Slots already spoken for by a real rekordbox hot cue.
        taken = {cue.hotcue for cue in track.cues if cue.hotcue >= 0}
        for cue in track.cues:
            hotcue, name = cue.hotcue, cue.name
            if cue.overflow:
                free = next((slot for slot in range(TRAKTOR_HOTCUE_SLOTS)
                             if slot not in taken), None) if remap_overflow else None
                if free is None:
                    report.overflow_cues += 1
                else:
                    hotcue = free
                    taken.add(free)
                    # It has a slot now, so the placeholder name is a lie.
                    name = name.replace(" (no Traktor slot)", "")
                    report.remapped_cues += 1
            cue_type = "5" if cue.length_ms > 0 else "0"
            elements.append(ET.Element(
                "CUE_V2", NAME=name, DISPL_ORDER="0", TYPE=cue_type,
                START=f"{cue.start_ms:.6f}", LEN=f"{cue.length_ms:.6f}",
                REPEATS="-1", HOTCUE=str(hotcue),
            ))
        return elements

    def _write_entry(self, entry: ET.Element, track: Track, *, new: bool,
                     cfg, report: SyncReport) -> bool:
        """Apply rekordbox data onto an ENTRY. Returns True if anything changed."""
        before = signature(entry)

        entry.set("TITLE", track.title)
        if track.artist:
            entry.set("ARTIST", track.artist)

        if new:
            volume, directory, filename = split_location(track.path, self.volume)
            ET.SubElement(entry, "LOCATION", DIR=directory, FILE=filename,
                          VOLUME=volume, VOLUMEID=volume)

        if track.album:
            album = entry.find("ALBUM")
            if album is None:
                album = ET.Element("ALBUM")
                entry.insert(1, album)
            album.set("TITLE", track.album)

        if entry.find("MODIFICATION_INFO") is None:
            ET.SubElement(entry, "MODIFICATION_INFO", AUTHOR_TYPE="importer")

        info = entry.find("INFO")
        if info is None:
            info = ET.SubElement(entry, "INFO")
        for key, value in self._info_attrs(track, cfg.sync_ratings, cfg.tags_into_comment).items():
            if key in self.FILL_ONLY_IF_ABSENT and info.get(key):
                continue
            info.set(key, value)

        if track.bpm:
            tempo = entry.find("TEMPO")
            if tempo is None:
                tempo = ET.SubElement(entry, "TEMPO")
            wanted_bpm = track.grid_bpm or track.bpm
            if not bpm_equal(tempo.get("BPM"), wanted_bpm):
                tempo.set("BPM", f"{wanted_bpm:.6f}")
            tempo.set("BPM_QUALITY", "100.000000")

        key_value = musical_key_value(track.key)
        if key_value is not None:
            musical = entry.find("MUSICAL_KEY")
            if musical is None:
                musical = ET.SubElement(entry, "MUSICAL_KEY")
            musical.set("VALUE", str(key_value))

        if cfg.sync_cues:
            desired = self._cue_elements(track, report, cfg.remap_overflow_cues)
            current = entry.findall("CUE_V2")
            # Traktor reorders cues on save. Compare them as a set, otherwise
            # every run would rewrite identical cues and report a false update.
            if cue_signature(current) != cue_signature(desired):
                for old in current:
                    entry.remove(old)
                for element in desired:
                    entry.append(element)

        if cfg.analysis_lock and is_analysed(entry) and set_analysis_lock(entry):
            report.locked += 1

        changed = new or signature(entry) != before
        if changed:
            # Only stamp the entry when something actually moved, otherwise every
            # run would look like a full update.
            now = datetime.now()
            entry.set("MODIFIED_DATE", f"{now.year}/{now.month}/{now.day}")
            entry.set("MODIFIED_TIME", str(now.hour * 3600 + now.minute * 60 + now.second))
        return changed

    # ------------------------------------------------------------ public API

    def merge_tracks(self, tracks: list[Track], cfg, report: SyncReport) -> None:
        existing = self.index()
        for track in tracks:
            if not track.path.exists():
                report.missing_files += 1
                continue
            entry = existing.get(track.match_key)
            if entry is None:
                entry = ET.SubElement(self.collection, "ENTRY")
                self._write_entry(entry, track, new=True, cfg=cfg, report=report)
                existing[track.match_key] = entry
                report.added += 1
            else:
                changed = self._write_entry(entry, track, new=False, cfg=cfg, report=report)
                report.updated += changed
                report.unchanged += not changed

        if cfg.remove_dead_files:
            for key, entry in list(existing.items()):
                if not Path(key).exists():
                    self.collection.remove(entry)
                    report.removed += 1

        self.collection.set("ENTRIES", str(len(self.collection.findall("ENTRY"))))

    def rebuild_playlists(self, playlists: list[Playlist], report: SyncReport,
                          renames: dict | None = None) -> None:
        """Refresh the rekordbox playlist tree in place.

        Traktor node names, their order and their UUIDs are kept, so renaming
        "Techno" to "1 Techno" for sorting survives every sync. Traktor-only
        nodes, at any depth, are left untouched.
        """
        renames = renames or {}
        container = self.root.find("PLAYLISTS")
        if container is None:
            container = ET.SubElement(self.root, "PLAYLISTS")
        root_node = next((n for n in container.findall("NODE")
                          if n.get("NAME") == "$ROOT"), None)
        if root_node is None:
            root_node = ET.SubElement(container, "NODE", TYPE="FOLDER", NAME="$ROOT")
        subnodes = root_node.find("SUBNODES")
        if subnodes is None:
            subnodes = ET.SubElement(root_node, "SUBNODES", COUNT="0")

        known = {entry_path(e) for e in self.collection.findall("ENTRY")}
        self._merge_level(subnodes, playlists, renames, report, known, "")

    def _merge_level(self, subnodes: ET.Element, playlists: list[Playlist],
                     renames: dict, report: SyncReport, known: set, prefix: str) -> None:
        children = list(subnodes)
        rebuilt: list[tuple[int | None, ET.Element]] = []
        matched: set[int] = set()

        for playlist in playlists:
            index, existing = match_node(children, playlist.name, renames)
            if existing is not None:
                matched.add(id(existing))
            display = existing.get("NAME") if existing is not None else playlist.name
            rebuilt.append((index, self._build_node(
                playlist, display, existing, renames, report, known, prefix)))

        replacements = {i: element for i, element in rebuilt if i is not None}
        result: list[ET.Element] = []
        for i, node in enumerate(children):
            if i in replacements:
                result.append(replacements[i])
            elif id(node) not in matched:
                result.append(node)          # Traktor's own, never ours to touch
        for i, element in rebuilt:
            if i is None:
                result.append(element)       # new in rekordbox since last sync

        subnodes[:] = result
        subnodes.set("COUNT", str(len(result)))

    def _build_node(self, playlist: Playlist, display: str, existing: ET.Element | None,
                    renames: dict, report: SyncReport, known: set,
                    prefix: str) -> ET.Element:
        if playlist.is_folder:
            node = ET.Element("NODE", TYPE="FOLDER", NAME=display)
            sub = ET.SubElement(node, "SUBNODES")
            if existing is not None:
                old = existing.find("SUBNODES")
                if old is not None:
                    for child in list(old):
                        sub.append(child)    # seed, so Traktor-only children survive
            self._merge_level(sub, playlist.children, renames, report, known,
                              f"{prefix}/{playlist.name}")
            return node

        node = ET.Element("NODE", TYPE="PLAYLIST", NAME=display)
        old_uuid = None
        if existing is not None:
            old_playlist = existing.find("PLAYLIST")
            if old_playlist is not None:
                old_uuid = old_playlist.get("UUID")
        listing = ET.SubElement(
            node, "PLAYLIST", TYPE="LIST",
            UUID=old_uuid or playlist_uuid(prefix, playlist.name))
        count = 0
        for path in playlist.tracks:
            if str(path).lower() not in known:
                report.playlist_tracks_missing += 1
                continue
            entry = ET.SubElement(listing, "ENTRY")
            ET.SubElement(entry, "PRIMARYKEY", TYPE="TRACK",
                          KEY=primary_key(path, self.volume))
            count += 1
        listing.set("ENTRIES", str(count))
        report.playlists += 1
        return node

    def save(self, backup_dir: Path, keep: int) -> Path | None:
        backup = None
        if self.path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"collection-{stamp}.nml"
            shutil.copy2(self.path, backup)
            old = sorted(backup_dir.glob("collection-*.nml"))[:-keep]
            for stale in old:
                stale.unlink(missing_ok=True)

        ET.indent(self.root, space="")
        tmp = self.path.with_suffix(".nml.djsync-tmp")
        self.tree.write(tmp, encoding="UTF-8", xml_declaration=True)
        os.replace(tmp, self.path)
        return backup
