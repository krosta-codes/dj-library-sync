# DJ Library Sync

**Keeps Traktor 4 in sync with your rekordbox library, cues, grids, playlists and all, so you can prep in one app and play in the other.**

[![License](https://img.shields.io/badge/license-Apache--2.0-black)](LICENSE) ![Platform](https://img.shields.io/badge/macOS-rekordbox%206%20%E2%86%92%20Traktor%204-black) [![Release](https://img.shields.io/github/v/release/krosta-codes/dj-library-sync)](https://github.com/krosta-codes/dj-library-sync/releases)

## Why this exists

I prep tracks in rekordbox and play out on Traktor. The usual route is an XML export plus a converter, repeated by hand after every prep session. So `djsync` reads rekordbox's own database directly and rewrites Traktor's `collection.nml` the way Traktor itself would. It runs on its own when you quit rekordbox. You never think about it again, which is the whole point.

## What it does

- Copies tracks, metadata, ratings, keys, hot cues, memory cues, loops, beatgrids and the full playlist tree from rekordbox 6 into Traktor 4.
- Reads a snapshot of `master.db` directly: no XML export, no GUI. **Never writes to rekordbox.**
- Runs in the background: syncs when rekordbox quits, and every 30 minutes as a fallback.
- Keeps what Traktor already knows (waveforms, cover art, loudness, Traktor-only tracks and playlists).
- Backs up `collection.nml` before every write and rolls back with one command.

## Install

| You need | Why |
|---|---|
| macOS with Python 3 (`python3`) | runs the sync |
| rekordbox 6 and Traktor 4, both on this Mac | the two libraries |

```bash
git clone https://github.com/krosta-codes/dj-library-sync.git ~/Projects/apps/dj-library-sync
~/Projects/apps/dj-library-sync/install.sh
```

The installer creates a virtualenv, installs [pyrekordbox](https://github.com/dylanljones/pyrekordbox), shows `djsync status` and offers to install the background agent. It finds `master.db` and the newest `Traktor */collection.nml` on its own; override both in `config.toml`.

## Use

```bash
djsync                 # sync (skips if nothing changed)
djsync --dry-run       # report what would change, write nothing
djsync status          # paths, pending changes, last run, backup count
djsync restore         # roll back to the newest backup
djsync restore --file backups/collection-20260920-152549.nml
djsync update          # pull the latest version
```

With the agent installed you never run it by hand.

## Updates

`djsync` checks this repo's releases at most once a day. In a terminal it prints one line when a new version is out; the background agent posts one notification per version instead. `djsync update` pulls your clone and refreshes the dependencies. The check is one request to the GitHub API and nothing else; turn it off with `UPDATE_CHECK=0`.

## Important: stop Traktor rewriting the imported beatgrids

Traktor still has to analyse each new track once, for the waveform stripe and
auto gain. Analysis also recalculates BPM and, by default, lays down its own
grid marker on top of the one this tool imported.

Look up three settings in your `Traktor Settings.tsi` (in the Traktor folder under `~/Documents/Native Instruments`). A setup where the first two are 0 is the safe case:

```
BackgroundThread.AutoAnalyze       = 0    nothing is analysed on import
BackgroundThread.AutoAnalyzeOnLoad = 0    nothing is analysed on deck load
Browser.BPM.AutomaticMarkers       = 1    analysis DOES place grid markers
```

With those values Traktor never touches a track until you explicitly analyse it. Then, in this
order:

1. **Preferences, Analyze Options, BPM Detection.** It is a dropdown, not a
   checkbox. Entries come in two families: ones ending in **"Set Beatgrid"**
   (for example "78 - 155 BPM, Set Beatgrid") and a **"No Markers"** entry,
   described in the app as "only determines the BPM of the track". Pick the
   **No Markers** entry. That stops analysis placing its own grid marker.
2. **Analyse the new tracks.** Select them, right click, Analyze (Async).
3. **Run `djsync` again.** Free, and it pushes the rekordbox BPM and grid back
   over anything analysis changed.
4. **Select all, right click, Analysis Lock.** In Traktor's own words: "Enable
   the Analysis Lock once Tempo, Beatgrid, Key and Gain of the Track are
   correct. This will lock these values against all future changes." This is the
   real protection, and it survives future syncs: djsync never writes the
   `FLAGS` attribute that stores it.

## How it protects your collection

- **Refuses to run while Traktor is open.** Traktor holds `collection.nml` in
  memory and rewrites it on quit, so any write while it runs is silently lost.
- **Refuses to run while rekordbox is open**, so it never reads a half-written
  database. Override with `--force`.
- **Backs up** `collection.nml` before every write, keeping the last 20.
- **Writes atomically** to a temp file, then renames.
- **Idempotent.** Two runs on unchanged data produce a byte-identical file.

## What is preserved on the Traktor side

Entries Traktor already knew keep their `AUDIO_ID`, `LOUDNESS`, cover art and
waveform references. Traktor-only tracks are never removed. Traktor-only
playlists, smart lists, `SETS` and `INDEXING` are left alone: only root nodes
whose name matches a rekordbox playlist get replaced.

## Field mapping

| rekordbox | Traktor |
|---|---|
| Title, Artist, Album, Genre, Label, Remixer | same fields |
| Rating 0-5 | `RANKING` 0-255, in steps of 51 |
| Key (Camelot or classical) | `INFO KEY` text plus `MUSICAL_KEY` 0-23 |
| Hot cues A-H | hot cue slots 1-8 |
| Hot cues I-P | plain cues, keeping position, labelled "no Traktor slot" |
| Memory cues | plain cues |
| Loops (cue with an out point) | `CUE_V2 TYPE="5"` with length |
| Beatgrid, first downbeat | `CUE_V2 TYPE="4"` plus `GRID BPM` |
| Colour, My Tags | `#hashtags` appended to the comment |

## Known quirks

- Variable beatgrids collapse to one marker plus a constant BPM.
- Traktor has 8 hot cue slots, rekordbox has 16. With `remap_overflow_cues`
  the extras land in free pads, if there are any free pads left.
- Cue colours do not transfer: Traktor colours cues by type.
- rekordbox smart playlists come across as empty lists.
- One way only. Edits made in Traktor are overwritten on the next sync. Treat rekordbox as the source of truth and Traktor as the stage.
- If a Homebrew Python upgrade breaks the virtualenv, the launcher notices and rebuilds it once on the next run.

## Configuration

`config.toml`. Useful switches:

- `skip_playlists`, `skip_playlist_prefixes`: leave History lists behind.
- `include_roots`: sync only tracks under given folders.
- `remove_dead_files`: delete Traktor entries whose file is gone. Off by default.
- `tags_into_comment`: turn off if you want the comment field untouched.
- `analysis_lock`: set Traktor's Analysis Lock on every track Traktor has
  already analysed, so analysis can never overwrite an imported grid. Writes
  both halves of the lock, `INFO FLAGS` bit 16 and `LOCK="1"` on the entry,
  which is what the Traktor UI does. Tracks without `AUDIO_ID` stay unlocked on
  purpose, a locked track is never analysed and would never get a waveform.
- `remap_overflow_cues`: a cue in rekordbox slot I or beyond goes into the first
  free Traktor pad instead of being dropped. Cues that already have a pad never
  move.

## Layout

```
djsync/rekordbox.py   read master.db and the ANLZ beatgrids
djsync/traktor.py     parse, merge and write collection.nml
djsync/cli.py         commands, safety guards, logging
djsync/update.py      daily release check, djsync update
VERSION               current version
config.toml           settings
backups/              rolling collection.nml backups
logs/djsync.log       every run
state.json            fingerprint of the last synced library
```

## Privacy

Everything runs on your Mac. The only network request is the daily update check to api.github.com (`UPDATE_CHECK=0` turns it off).

## Troubleshooting

**"rekordbox database not found"** or a key error after a rekordbox update:

```bash
~/Projects/apps/dj-library-sync/.venv/bin/python -m pyrekordbox download-key
```

**Nothing happens.** `djsync status` shows whether changes are pending and
whether an app is blocking the run. Force one with `djsync --force`.

**Agent logs:** `logs/launchd.err.log` and `logs/djsync.log`.

---

Built by [Jurek Krosta](https://jurekkrosta.com) ("AI Slop - aber richtig").
Licensed under Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
The name "DJ Library Sync" and the Krosta brand are not covered by the license, see [TRADEMARK.md](TRADEMARK.md).
Need this built for your shop or team? [Contact](https://jurekkrosta.com).
