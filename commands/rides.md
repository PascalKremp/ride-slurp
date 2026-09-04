---
description: Sync a Garmin device and export rides as GPX, CSV, JSON or a markdown log.
argument-hint: [sync | gpx | csv | log | stats]
---

Use the `ride-slurp` skill: $ARGUMENTS

If no argument is given, run `detect` then `sync`, and report the new rides.

Choose the output format from what the user actually wants to do with it — GPX for maps
and Strava, CSV for spreadsheets, JSON for scripting, markdown only for a log a person
reads. Don't default to markdown.
