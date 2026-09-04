---
name: ride-slurp
description: "Pull activities off a Garmin bike computer or watch over USB and turn them into something usable - GPX tracks for Strava/Komoot/maps, a CSV of every ride for spreadsheets, JSON, or a human-readable markdown training log per month. Also shows live heart rate from any Bluetooth LE chest strap. Use when the user says 'sync my Garmin', 'pull my rides off the Edge', 'I plugged in my bike computer', 'export my rides to GPX', 'update my training log', or asks for cycling stats (km, elevation, FTP, TSS, watts, heart rate) from recorded activities."
---

# ride-slurp: Garmin device → GPX / CSV / training log

Mirrors a Garmin device over USB and turns the `.fit` activity files into whatever
format is actually useful for the question being asked.

## The one thing to know

**Most Garmin devices are MTP, not USB mass storage.** The operating system will never
mount one as a volume — `/Volumes`, `diskutil list` and `lsblk` show nothing, no matter
how long you wait. Do not go looking for a drive; everything goes through libmtp.

## Setup (idempotent)

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/ride-slurp/scripts/setup.sh"
```

Prints `READY <python-path>`. Creates a venv at `~/.local/share/ride-slurp/venv` with
`fitdecode` (+ `bleak` for live heart rate). It warns if **libmtp** is missing — that's
only needed for `detect`/`sync`, not for reading an existing mirror.

libmtp: `brew install libmtp` (macOS) / `sudo apt install mtp-tools` (Debian/Ubuntu).

## Commands

```bash
PY=~/.local/share/ride-slurp/venv/bin/python
S="${CLAUDE_PLUGIN_ROOT}/skills/ride-slurp/scripts/ride_slurp.py"

$PY $S detect                                  # plugged in? model, serial, firmware
$PY $S sync                                    # pull only what the mirror lacks
$PY $S sync --dry-run                          # list what would be pulled
$PY $S rides                                   # table of every ride
$PY $S rides --format csv --since 2026-08-01   # table | json | csv | markdown
$PY $S export --format gpx --out ./gpx         # one .gpx per ride
$PY $S export --format csv --out rides.csv     # all rides, one spreadsheet
$PY $S log --out-dir ./ride-log                # markdown, one file per month
```

All read commands accept `--since` / `--until` / `--limit` / `--mirror`.

## Pick the right output format

**Do not default to markdown.** Match the format to what the user is going to do:

| They want to… | Use |
|---|---|
| Upload a ride to Strava / Komoot, open it on a map, share a route | `export --format gpx` |
| Analyse numbers, pivot, chart, open in Excel/Numbers | `export --format csv` |
| Feed another script or answer a question yourself | `rides --format json` |
| Read a training log, keep notes in a knowledge base | `log` (markdown) |
| Just see what's there, in the terminal | `rides` (default table) |

Markdown is for the things a person reads. Ride data is data — give them GPX or CSV.

## Normal sync run

1. `detect` — confirm the device is there. If not: plug in USB **and unlock the screen**;
   a locked device won't enumerate.
2. `sync` — incremental. Lists the device (~20 s), diffs against the mirror by path +
   size, pulls only what's new. A typical post-ride sync is 1–2 files.
3. Export or log, per the table above.
4. Report the new rides: distance, moving time, ascent, NP/TSS.

## GPX export

One file per ride, named `<date>-<time>-<profile>.gpx`, containing the full GPS track
with elevation, timestamps, and — where the device recorded them — heart rate, cadence,
power and temperature (Garmin `TrackPointExtension`, which is what Strava and Komoot read).

Indoor rides and trainer sessions have no GPS points; they're reported in the result's
`skipped` list rather than written as empty files. The last stdout line is JSON with
`written`, `skipped` and per-file point counts.

## Markdown log

`log --out-dir <dir>` writes `rides-YYYY-MM.md` per month plus an index, and **rewrites
every month on each run**, so a backfilled old ride lands in the right file automatically.

- `--link-style plain` (default) — ordinary relative markdown links. Works anywhere.
- `--link-style obsidian` — `[[wiki-links]]` and `_index.md`, for an Obsidian vault.
- `--lang en` (default) or `--lang de` — headings and number formatting (`1.234,5`).
- `--prefix rides` — filename prefix for the monthly files.

Don't hand-edit the generated files; they're overwritten on the next run.

## Live heart rate

```bash
$PY "${CLAUDE_PLUGIN_ROOT}/skills/ride-slurp/scripts/hr_live.py"          # live display
$PY "${CLAUDE_PLUGIN_ROOT}/skills/ride-slurp/scripts/hr_live.py" --scan   # what's visible
$PY "${CLAUDE_PLUGIN_ROOT}/skills/ride-slurp/scripts/hr_live.py" --raw    # one line per reading
```

Works with any BLE chest strap (standard Heart Rate service 0x180D). Dual-band straps
broadcast ANT+ and BLE at once, so this doesn't disturb the pairing to the bike computer.

**The strap only wakes up when worn** — a scan with it on the desk finds nothing. This is
a continuous display, so let the **user** run it in their own terminal rather than making
a blocking tool call.

## Gotchas that cost time once already

- **Batch the transfers.** `mtp-connect` takes many `--getfile <id> <dest>` pairs in one
  MTP session. One process per file pays ~2 s of setup each — 328 files went from ~20 min
  to 58 s by batching 40 per call. The script already does this.
- **`mtp-connect`'s exit code lies.** It prints `Unknown options: <paths>` after a
  successful multi-getfile run. Verify transfers by comparing file size against the device
  listing, never by return code. The script already does this.
- **Skip map tiles and SQL caches** unless asked — tens of GB of regenerable device
  internals and an hour of transfer. `sync` excludes them; `--all` includes them.
- **`mtp-folders` output is ~90 % noise.** The real tree is the `<id>\t<indented name>`
  lines; indentation is 2 spaces per level and encodes the parent chain.
- **`.fit` timestamps are UTC.** The `activity` message carries `local_timestamp` next to
  `timestamp`; the difference is the device's offset. The script applies it, so ride times
  read as local. GPX keeps UTC, as the format requires.
- **Positions are semicircles**, not degrees (`degrees = semicircles × 180/2³¹`). Handled.

## Data available per ride

Distance, elapsed/moving time, avg+max speed, ascent/descent, avg+max HR, avg+max power,
normalized power, FTP, TSS, intensity factor, calories, cadence, temperature, aerobic and
anaerobic training effect.
