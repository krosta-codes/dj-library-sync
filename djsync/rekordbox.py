"""Read the rekordbox 6 library into a neutral model.

Never touches the live database: it snapshots master.db (+ WAL) and the
analysis files into a temp directory and opens the copy.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pyrekordbox import AnlzFile, Rekordbox6Database

log = logging.getLogger("djsync")

# rekordbox DjmdCue.Kind: 0 = memory cue, 1..n = hot cue A, B, C ...
HOTCUE_LETTERS = "ABCDEFGHIJKLMNOP"


@dataclass
class Cue:
    start_ms: float
    length_ms: float          # > 0 means it is a loop
    hotcue: int               # 0..7 for a Traktor hot cue slot, -1 for a plain cue
    name: str
    overflow: bool = False    # rekordbox slot I and beyond: no Traktor slot exists


@dataclass
class Track:
    path: Path
    title: str
    artist: str
    album: str = ""
    genre: str = ""
    label: str = ""
    remixer: str = ""
    comment: str = ""
    key: str = ""
    bpm: float = 0.0
    length_s: float = 0.0
    bitrate: int = 0
    filesize_kb: int = 0
    rating: int = 0           # 0..5
    playcount: int = 0
    release_date: str = ""    # YYYY/M/D
    import_date: str = ""     # YYYY/M/D
    color: str = ""
    mytags: list[str] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    grid_start_ms: float | None = None
    grid_bpm: float | None = None

    @property
    def match_key(self) -> str:
        return str(self.path).lower()


@dataclass
class Playlist:
    name: str
    is_folder: bool
    seq: int
    children: list["Playlist"] = field(default_factory=list)
    tracks: list[Path] = field(default_factory=list)


def _date_string(value) -> str:
    if not value:
        return ""
    try:
        return f"{value.year}/{value.month}/{value.day}"
    except AttributeError:
        text = str(value)[:10].replace("-", "/")
        parts = [p.lstrip("0") or "0" for p in text.split("/")]
        return "/".join(parts) if len(parts) == 3 else ""


class RekordboxSnapshot:
    """Context manager around a throwaway copy of the rekordbox library."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._tmp: Path | None = None
        self.db: Rekordbox6Database | None = None

    def __enter__(self) -> "RekordboxSnapshot":
        self._tmp = Path(tempfile.mkdtemp(prefix="djsync-rb-"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.db_path) + suffix)
            if src.exists():
                shutil.copy2(src, self._tmp / src.name)
        # Analysis files hold the beatgrids; symlink so we do not copy gigabytes.
        share = self.db_path.parent / "share"
        if share.is_dir():
            (self._tmp / "share").symlink_to(share)
        self.db = Rekordbox6Database(path=self._tmp / self.db_path.name, unlock=True)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.db is not None:
                self.db.close()
        except Exception:
            pass
        if self._tmp and self._tmp.exists():
            shutil.rmtree(self._tmp, ignore_errors=True)

    # ---------------------------------------------------------------- tracks

    def _beatgrid(self, content) -> tuple[float | None, float | None]:
        """First downbeat in ms and its tempo, read from the ANLZ .DAT file."""
        try:
            paths = self.db.get_anlz_paths(content)
            dat = paths.get("DAT")
            if not dat or not Path(dat).exists():
                return None, None
            anlz = AnlzFile.parse_file(dat)
            if "PQTZ" not in [tag.type for tag in anlz.tags]:
                return None, None
            beats, tempos, times = anlz.get("beat_grid")
            if len(beats) == 0:
                return None, None
            # pyrekordbox returns beat times in seconds and tempo in real BPM.
            # Traktor wants the grid marker in milliseconds.
            for index, beat in enumerate(beats):
                if beat == 1:
                    return float(times[index]) * 1000.0, float(tempos[index])
            return float(times[0]) * 1000.0, float(tempos[0])
        except Exception as exc:
            log.debug("no beatgrid for %s: %s", content.Title, exc)
            return None, None

    def _cues(self, content) -> list[Cue]:
        cues: list[Cue] = []
        for raw in sorted(content.Cues, key=lambda c: c.InMsec or 0):
            kind = raw.Kind or 0
            start = float(raw.InMsec or 0)
            out = float(raw.OutMsec or 0)
            length = out - start if out > start else 0.0

            if kind == 0:
                hotcue, overflow = -1, False
                default = "Memory"
            elif kind <= 8:
                hotcue, overflow = kind - 1, False
                default = HOTCUE_LETTERS[kind - 1]
            else:
                # rekordbox slots I..P have no Traktor equivalent. Keep the
                # position as a plain cue rather than dropping it.
                hotcue, overflow = -1, True
                letter = HOTCUE_LETTERS[kind - 1] if kind <= len(HOTCUE_LETTERS) else str(kind)
                default = f"{letter} (no Traktor slot)"

            name = (raw.Comment or "").strip() or default
            cues.append(Cue(start, length, hotcue, name, overflow))
        return cues

    def tracks(self, include_roots: list[str] | None = None) -> list[Track]:
        out: list[Track] = []
        roots = [r.rstrip("/") for r in (include_roots or [])]
        for content in self.db.get_content():
            folder = content.FolderPath
            if not folder:
                continue
            if roots and not any(folder.startswith(r + "/") for r in roots):
                continue

            track = Track(
                path=Path(folder),
                title=(content.Title or Path(folder).stem).strip(),
                artist=(content.ArtistName or "").strip(),
                album=(content.AlbumName or "").strip(),
                genre=(content.GenreName or "").strip(),
                label=(content.LabelName or "").strip(),
                remixer=(content.RemixerName or "").strip(),
                comment=(content.Commnt or "").strip(),
                key=(content.KeyName or "").strip(),
                bpm=(content.BPM or 0) / 100.0,
                length_s=float(content.Length or 0),
                bitrate=int(content.BitRate or 0) * 1000,
                filesize_kb=int((content.FileSize or 0) / 1024),
                rating=int(content.Rating or 0),
                playcount=int(content.DJPlayCount or 0),
                release_date=_date_string(content.ReleaseDate),
                import_date=_date_string(content.created_at),
                color=(content.ColorName or "").strip(),
                mytags=[t for t in (content.MyTagNames or []) if t],
            )
            track.cues = self._cues(content)
            if track.bpm:
                track.grid_start_ms, track.grid_bpm = self._beatgrid(content)
                if track.grid_bpm is None and track.cues:
                    track.grid_bpm = track.bpm
            out.append(track)
        return out

    # ------------------------------------------------------------- playlists

    def playlists(self, skip: list[str], skip_prefixes: list[str]) -> list[Playlist]:
        skip_lower = {s.lower() for s in skip}
        prefixes = tuple(p.lower() for p in skip_prefixes)

        by_parent: dict[str, list] = {}
        for node in self.db.get_playlist():
            by_parent.setdefault(str(node.ParentID), []).append(node)

        def skipped(name: str) -> bool:
            low = name.lower()
            return low in skip_lower or low.startswith(prefixes)

        def build(parent_id: str) -> list[Playlist]:
            result = []
            for node in sorted(by_parent.get(parent_id, []), key=lambda n: n.Seq or 0):
                name = (node.Name or "Unnamed").strip()
                if skipped(name):
                    continue
                is_folder = node.Attribute == 1
                pl = Playlist(name=name, is_folder=is_folder, seq=node.Seq or 0)
                if is_folder:
                    pl.children = build(str(node.ID))
                else:
                    pl.tracks = [
                        Path(song.Content.FolderPath)
                        for song in sorted(node.Songs, key=lambda s: s.TrackNo or 0)
                        if song.Content and song.Content.FolderPath
                    ]
                result.append(pl)
            return result

        roots = [k for k in ("root", "0", "None") if k in by_parent]
        nodes: list[Playlist] = []
        for key in roots:
            nodes.extend(build(key))
        return nodes
