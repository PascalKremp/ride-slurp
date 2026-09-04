# ride-slurp

A [Claude Code](https://claude.com/claude-code) plugin that pulls rides off a Garmin bike
computer or watch over USB and gives you them in a format you can actually use — **GPX**
tracks for Strava, Komoot and maps, a **CSV** of every ride for spreadsheets, JSON, or a
readable **markdown training log**.

```
/rides                          # detect, sync, report new rides
/rides export my August to gpx
```

## Why

Garmin Connect owns your rides and gives them back grudgingly. The device itself is
sitting right there on USB with every `.fit` file on it — but most Garmins speak **MTP**,
not USB mass storage, so the device never mounts as a drive and the files look
unreachable.

This mirrors the device over libmtp, parses the `.fit` files, and exports.
Everything stays on your machine; no Garmin account, no cloud round-trip.

## Install

```
/plugin marketplace add PascalKremp/ride-slurp
/plugin install ride-slurp
```

## Requirements

- **libmtp** — `brew install libmtp` (macOS) / `sudo apt install mtp-tools` (Debian/Ubuntu).
  Only needed to talk to the device; reading an existing mirror doesn't need it.
- **Python 3.9+** — `setup.sh` builds a venv with `fitdecode` (and `bleak` for live HR).

## Output formats

Ride data is data. Markdown is only for the log a person reads.

| Format | Command | For |
|---|---|---|
| **GPX** | `export --format gpx --out ./gpx` | Strava, Komoot, any map tool |
| **CSV** | `export --format csv --out rides.csv` | Excel, Numbers, pandas |
| **JSON** | `rides --format json` | scripting, further processing |
| **Markdown** | `log --out-dir ./ride-log` | a training log you actually read |
| **Table** | `rides` | a quick look in the terminal |

### GPX

One file per ride, with the full track: elevation, timestamps, and — where the device
recorded them — heart rate, cadence, power and temperature, in the Garmin
`TrackPointExtension` that Strava and Komoot read. Indoor/trainer rides with no GPS are
reported as skipped instead of written as empty files.

### Markdown log

One file per month plus an index, regenerated on every run so a backfilled ride lands in
the right month automatically.

- `--link-style plain` (default) — ordinary markdown links, works anywhere
- `--link-style obsidian` — `[[wiki-links]]` and `_index.md` for an Obsidian vault
- `--lang en` (default) / `--lang de` — headings and number formatting

## Commands

```bash
PY=~/.local/share/ride-slurp/venv/bin/python
S=skills/ride-slurp/scripts/ride_slurp.py

$PY $S detect                                # plugged in? model, serial, firmware
$PY $S sync                                  # incremental pull off the device
$PY $S sync --dry-run                        # what would be pulled
$PY $S rides --since 2026-08-01              # summarize
$PY $S export --format gpx --out ./gpx
$PY $S log --out-dir ./ride-log --lang de
```

`--since` / `--until` / `--limit` / `--mirror` work on every read command. Export and log
print a JSON summary as their last stdout line, so they compose in a pipeline.

## Live heart rate

```bash
$PY skills/ride-slurp/scripts/hr_live.py         # live bpm, min/avg/max
$PY skills/ride-slurp/scripts/hr_live.py --scan  # which straps are visible
$PY skills/ride-slurp/scripts/hr_live.py --raw   # one line per reading, for logging
```

Works with any Bluetooth LE chest strap (standard Heart Rate service `0x180D`), not just
Garmin. Dual-band straps broadcast ANT+ and BLE simultaneously, so reading BLE here does
not disturb the strap's pairing with your bike computer.

Note: most straps only wake up once they're actually worn — a scan with the strap on your
desk will find nothing.

## Notes

- `sync` skips map tiles and SQL caches by default — tens of GB of regenerable device
  internals. Pass `--all` if you really want them.
- Transfers are batched into single MTP sessions (~20× faster) and verified by file size,
  because `mtp-connect`'s exit code is unreliable.
- Ride times are shown in the device's local time; GPX keeps UTC, as the format requires.

## License

MIT
