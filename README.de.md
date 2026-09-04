# ride-slurp

**Fahrten per USB vom Garmin holen und als GPX, CSV, JSON oder Trainingslog exportieren.**

[English](README.md) · [Deutsch](README.de.md)

---

Ein [Claude Code](https://claude.com/claude-code)-Plugin, das Fahrten per USB von einem
Garmin-Radcomputer oder einer Garmin-Uhr holt und sie in einem Format zurückgibt, mit dem
man wirklich etwas anfangen kann: **GPX**-Tracks für Strava, Komoot und Karten, eine **CSV**
aller Fahrten für die Tabellenkalkulation, JSON, oder ein lesbares **Markdown-Trainingslog**.

```
/rides                            # erkennen, synchronisieren, neue Fahrten melden
/rides exportiere den August als gpx
```

## Warum

Garmin Connect besitzt deine Fahrten und rückt sie nur widerwillig heraus. Dabei liegt das
Gerät direkt per USB an, mit jeder `.fit`-Datei darauf — aber die meisten Garmins sprechen
**MTP** statt USB-Massenspeicher. Das Gerät taucht also nie als Laufwerk auf, und die
Dateien wirken unerreichbar.

Dieses Plugin spiegelt das Gerät über libmtp, liest die `.fit`-Dateien und exportiert.
Alles bleibt auf deinem Rechner — kein Garmin-Konto, kein Umweg über die Cloud.

## Installation

```
/plugin marketplace add PascalKremp/ride-slurp
/plugin install ride-slurp
```

## Voraussetzungen

- **libmtp** — `brew install libmtp` (macOS) / `sudo apt install mtp-tools` (Debian/Ubuntu).
  Nur nötig, um mit dem Gerät zu sprechen; ein vorhandenes Spiegelverzeichnis zu lesen
  braucht es nicht.
- **Python 3.9+** — `setup.sh` baut ein venv mit `fitdecode` (und `bleak` für den Live-Puls).

## Ausgabeformate

Fahrdaten sind Daten. Markdown ist nur für das Log, das ein Mensch liest.

| Format | Befehl | Wofür |
|---|---|---|
| **GPX** | `export --format gpx --out ./gpx` | Strava, Komoot, jedes Kartenwerkzeug |
| **CSV** | `export --format csv --out fahrten.csv` | Excel, Numbers, pandas |
| **JSON** | `rides --format json` | Skripte, Weiterverarbeitung |
| **Markdown** | `log --out-dir ./fahrtenlog` | ein Trainingslog, das man tatsächlich liest |
| **Tabelle** | `rides` | schneller Blick im Terminal |

### GPX

Eine Datei pro Fahrt, mit dem vollständigen Track: Höhe, Zeitstempel und — sofern das Gerät
sie aufgezeichnet hat — Herzfrequenz, Trittfrequenz, Leistung und Temperatur, in der
Garmin-`TrackPointExtension`, die Strava und Komoot auswerten. Indoor- und Rollenfahrten
ohne GPS werden als übersprungen gemeldet, statt als leere Dateien geschrieben.

### Markdown-Log

Eine Datei pro Monat plus Index, bei jedem Lauf neu erzeugt — eine nachträglich
hinzugekommene alte Fahrt landet also automatisch im richtigen Monat.

- `--link-style plain` (Standard) — normale Markdown-Links, funktioniert überall
- `--link-style obsidian` — `[[Wiki-Links]]` und `_index.md` für einen Obsidian-Vault
- `--lang en` (Standard) / `--lang de` — Überschriften und Zahlenformat

## Befehle

```bash
PY=~/.local/share/ride-slurp/venv/bin/python
S=skills/ride-slurp/scripts/ride_slurp.py

$PY $S detect                                # angeschlossen? Modell, Seriennummer, Firmware
$PY $S sync                                  # inkrementell vom Gerät holen
$PY $S sync --dry-run                        # was geholt würde
$PY $S rides --since 2026-08-01              # zusammenfassen
$PY $S export --format gpx --out ./gpx
$PY $S log --out-dir ./fahrtenlog --lang de
```

`--since` / `--until` / `--limit` / `--mirror` funktionieren bei jedem Lesebefehl. Export
und Log geben als letzte stdout-Zeile eine JSON-Zusammenfassung aus und lassen sich in
Pipelines einbauen.

## Live-Herzfrequenz

```bash
$PY skills/ride-slurp/scripts/hr_live.py         # Live-Puls, min/ø/max
$PY skills/ride-slurp/scripts/hr_live.py --scan  # welche Gurte sichtbar sind
$PY skills/ride-slurp/scripts/hr_live.py --raw   # eine Zeile pro Messwert, zum Loggen
```

Funktioniert mit jedem Bluetooth-LE-Brustgurt (Standard-Heart-Rate-Service `0x180D`), nicht
nur mit Garmin. Dual-Band-Gurte senden ANT+ und BLE gleichzeitig, das Mitlesen über BLE
stört die Kopplung mit dem Radcomputer also nicht.

Hinweis: Die meisten Gurte wachen erst auf, wenn sie getragen werden — ein Scan mit dem
Gurt auf dem Schreibtisch findet nichts.

## Hinweise

- `sync` überspringt standardmäßig Kartenkacheln und SQL-Caches — zig Gigabyte
  regenerierbare Geräteinterna. Mit `--all` holst du sie trotzdem.
- Übertragungen werden in einzelne MTP-Sitzungen gebündelt (~20× schneller) und über die
  Dateigröße verifiziert, weil der Exit-Code von `mtp-connect` unzuverlässig ist.
- Fahrtzeiten werden in der Lokalzeit des Geräts angezeigt; GPX bleibt bei UTC, wie das
  Format es verlangt.

## Lizenz

MIT
