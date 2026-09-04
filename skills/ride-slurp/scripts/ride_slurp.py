#!/usr/bin/env python3
"""ride_slurp.py - pull activities off a Garmin MTP device and export them.

Many Garmin devices (Edge 1050 and friends) speak MTP, not USB mass storage,
so the operating system never mounts them as a drive. Everything here goes
through libmtp.

The trick that makes syncing fast: mtp-connect accepts many --getfile pairs in
a SINGLE MTP session. One process per file costs ~2s of session setup each;
batching turned a ~20 minute pull into ~60 seconds.

Commands:
    detect                    is a device plugged in, and what is it
    sync                      mirror new files off the device (incremental)
    rides                     summarize recorded activities (table/json/csv)
    export --format gpx       one .gpx track per ride (Strava, Komoot, maps)
    export --format csv       every ride as one spreadsheet-ready .csv
    log                       human-readable markdown, one file per month

`detect` and `sync` need libmtp on PATH. `rides`, `export` and `log` only read
the local mirror and need `fitdecode`.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import xml.sax.saxutils as sax

DEFAULT_ROOT = os.path.expanduser(
    os.environ.get("RIDE_SLURP_HOME", "~/.local/share/ride-slurp"))
# Device mirrors live in their own subtree so they can never be confused with
# the sibling venv setup.sh creates under the same root.
MIRROR_ROOT = os.path.join(DEFAULT_ROOT, "devices")

# Regenerable device internals - tens of GB of map tiles and SQLite caches on
# a stock Edge 1050. Never worth mirroring.
BULK = ("Garmin/Maps/", "Garmin/SQL/internal/")

CHUNK = 40  # --getfile pairs per mtp-connect session

SEMICIRCLE = 180.0 / (2 ** 31)


# ------------------------------------------------------------- libmtp ------

def mtp_tool(name: str) -> str | None:
    """Find an libmtp binary without assuming a package manager's prefix."""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        candidate = os.path.join(prefix, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def require_mtp() -> tuple[str, str, str, str] | None:
    tools = [mtp_tool(n) for n in
             ("mtp-detect", "mtp-files", "mtp-folders", "mtp-connect")]
    if all(tools):
        return tuple(tools)  # type: ignore[return-value]
    hint = ("macOS:         brew install libmtp\n"
            "  Debian/Ubuntu: sudo apt install mtp-tools\n"
            "  Fedora:        sudo dnf install libmtp")
    print(f"libmtp not found on PATH. Install it:\n  {hint}", file=sys.stderr)
    return None


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd="/tmp", capture_output=True,
                          text=True, timeout=timeout)


def usb_present(mtp_detect: str) -> list[tuple[str, str]]:
    """Garmin USB devices. Uses a cheap OS probe where one exists."""
    system = platform.system()
    if system == "Darwin":
        out = _run(["ioreg", "-p", "IOUSB", "-w0", "-l"], timeout=30).stdout
        found, product = [], None
        for line in out.splitlines():
            m = re.search(r'"USB Product Name" = "([^"]+)"', line)
            if m:
                product = m.group(1)
            m = re.search(r'"USB Vendor Name" = "([^"]+)"', line)
            if m and m.group(1).lower() == "garmin" and product:
                found.append((m.group(1), product))
        return found
    if system == "Linux" and shutil.which("lsusb"):
        out = _run(["lsusb"], timeout=30).stdout
        return [("Garmin", line.split(":", 2)[-1].strip())
                for line in out.splitlines() if "garmin" in line.lower()]
    # No cheap probe available - fall back to the MTP handshake itself.
    out = _run([mtp_detect], timeout=180).stdout
    return [("Garmin", "MTP device")] if "Model:" in out else []


def device_info(mtp_detect: str) -> dict:
    """Model + serial straight from the MTP handshake."""
    out = _run([mtp_detect], timeout=180).stdout
    info = {}
    for key, field in (("Manufacturer", "manufacturer"), ("Model", "model"),
                       ("Serial number", "serial"),
                       ("Device version", "firmware")):
        m = re.search(rf"^\s*{key}:\s*(.+?)\s*$", out, re.M)
        if m:
            info[field] = m.group(1)
    return info


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def list_device(mtp_files: str, mtp_folders: str) -> list[dict]:
    """Full file inventory off the device: path, size, MTP object id."""
    folders_out = _run([mtp_folders]).stdout
    files_out = _run([mtp_files]).stdout

    # mtp-folders output is mostly noise; the real tree is the
    # "<id>\t<2-space-per-level indent><name>" lines, where the indentation
    # encodes the parent chain.
    folders, order = {}, []
    for line in folders_out.splitlines():
        m = re.match(r"^(\d+)\t(\s*)(.+?)\s*$", line)
        if not m:
            continue
        fid = int(m.group(1))
        folders[fid] = {"name": m.group(3), "depth": len(m.group(2)) // 2}
        order.append(fid)

    stack = {}
    for fid in order:
        d = folders[fid]["depth"]
        stack[d] = fid
        folders[fid]["parent"] = stack.get(d - 1) if d else None

    def path_of(fid):
        parts = []
        while fid is not None and fid in folders:
            parts.append(folders[fid]["name"])
            fid = folders[fid]["parent"]
        return "/".join(reversed(parts))

    files, cur = [], {}
    for line in files_out.splitlines():
        s = line.strip()
        if line.startswith("File ID:"):
            if cur.get("id") is not None:
                files.append(cur)
            cur = {"id": int(line.split(":")[1])}
        elif s.startswith("Filename:"):
            cur["name"] = line.split("Filename:", 1)[1].strip()
        elif s.startswith("File size"):
            cur["size"] = int(re.search(r"File size (\d+)", line).group(1))
        elif s.startswith("Parent ID:"):
            cur["parent"] = int(line.split(":")[1])
    if cur.get("id") is not None:
        files.append(cur)

    for f in files:
        f["path"] = (path_of(f.get("parent")) + "/" + f["name"]).lstrip("/")
    return [f for f in files if f.get("size") is not None]


# --------------------------------------------------------------- mirror ----

def resolve_mirror(explicit: str | None, model: str | None = None) -> str:
    """Where the device mirror lives.

    Explicit --mirror wins. Otherwise use <root>/<model-slug> if we know the
    model, else the single existing mirror under the root. Guessing a device
    name would silently read the wrong device on a multi-device setup.
    """
    if explicit:
        return os.path.expanduser(explicit)
    if model:
        return os.path.join(MIRROR_ROOT, slug(model))
    if os.path.isdir(MIRROR_ROOT):
        # Only directories that actually hold a device mirror count.
        dirs = [d for d in sorted(os.listdir(MIRROR_ROOT))
                if os.path.isdir(os.path.join(MIRROR_ROOT, d, "Garmin"))]
        if len(dirs) == 1:
            return os.path.join(MIRROR_ROOT, dirs[0])
        if len(dirs) > 1:
            raise SystemExit(
                "Several device mirrors exist - pass --mirror to pick one:\n  " +
                "\n  ".join(os.path.join(MIRROR_ROOT, d) for d in dirs))
    raise SystemExit(f"No device mirror found under {MIRROR_ROOT} - run `sync` "
                     "first, or pass --mirror to point at an existing one.")


def cmd_sync(args) -> int:
    tools = require_mtp()
    if not tools:
        return 2
    mtp_detect, mtp_files, mtp_folders, mtp_connect = tools

    if not usb_present(mtp_detect):
        print("No Garmin device on USB. Plug it in and unlock the screen "
              "(a locked device will not enumerate).", file=sys.stderr)
        return 1

    info = device_info(mtp_detect)
    model = info.get("model", "garmin")
    print(f"device: {info.get('manufacturer','?')} {model} "
          f"(SN {info.get('serial','?')}, fw {info.get('firmware','?')})")

    mirror = resolve_mirror(args.mirror, model)

    print("listing device...", flush=True)
    files = list_device(mtp_files, mtp_folders)
    if not args.all:
        files = [f for f in files if not f["path"].startswith(BULK)]
    files.sort(key=lambda f: f["path"])

    todo = []
    for f in files:
        local = os.path.join(mirror, f["path"])
        if not os.path.exists(local) or os.path.getsize(local) != f["size"]:
            todo.append(f)

    have = len(files) - len(todo)
    size = sum(f["size"] for f in todo)
    print(f"device: {len(files)} files | mirror already has {have} | "
          f"to pull: {len(todo)} ({size/1e6:.1f} MB)")
    if args.dry_run:
        for f in todo[:50]:
            print(f"  + {f['path']}  ({f['size']/1024:.0f} KB)")
        if len(todo) > 50:
            print(f"  ... and {len(todo)-50} more")
        return 0
    if not todo:
        print("already up to date.")
        return 0

    for f in todo:
        os.makedirs(os.path.join(mirror, os.path.dirname(f["path"])), exist_ok=True)

    ok = fail = 0
    t0 = time.time()
    for i in range(0, len(todo), CHUNK):
        batch = todo[i:i + CHUNK]
        cmd = [mtp_connect]
        for f in batch:
            cmd += ["--getfile", str(f["id"]), os.path.join(mirror, f["path"])]
        _run(cmd)
        # mtp-connect's exit code is unreliable - it warns "Unknown options"
        # on the trailing paths even after a successful run. Verify by size.
        for f in batch:
            p = os.path.join(mirror, f["path"])
            if os.path.exists(p) and os.path.getsize(p) == f["size"]:
                ok += 1
            else:
                fail += 1
                got = os.path.getsize(p) if os.path.exists(p) else "MISSING"
                print(f"  FAIL {f['path']} want {f['size']} got {got}")
        print(f"  [{ok+fail}/{len(todo)}] ok={ok} fail={fail} "
              f"({time.time()-t0:.0f}s)", flush=True)

    print(f"\n{ok} pulled, {fail} failed, {time.time()-t0:.0f}s -> {mirror}")
    return 1 if fail else 0


def cmd_detect(args) -> int:
    tools = require_mtp()
    if not tools:
        return 2
    mtp_detect = tools[0]
    devs = usb_present(mtp_detect)
    if not devs:
        print("No Garmin device on USB.")
        return 1
    for vendor, product in devs:
        print(f"USB: {vendor} {product}")
    for k, v in device_info(mtp_detect).items():
        print(f"  {k}: {v}")
    return 0


# ------------------------------------------------------------------ fit ----

def _local_offset(activity: dict):
    """FIT timestamps are UTC; the activity message also carries local time,
    so the difference is the device's UTC offset at record time."""
    if activity and activity.get("local_timestamp") and activity.get("timestamp"):
        try:
            return (activity["local_timestamp"].replace(tzinfo=None)
                    - activity["timestamp"].replace(tzinfo=None))
        except Exception:
            return None
    return None


def parse_fit(path: str) -> dict | None:
    """Session summary out of one .fit activity file."""
    import fitdecode

    session = activity = None
    try:
        with fitdecode.FitReader(path) as fr:
            for frame in fr:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name == "session" and session is None:
                    session = {f.name: f.value for f in frame.fields}
                elif frame.name == "activity" and activity is None:
                    activity = {f.name: f.value for f in frame.fields}
    except Exception as e:
        return {"file": os.path.basename(path), "error": str(e)}
    if not session:
        return None

    start = session.get("start_time")
    off = _local_offset(activity)
    if start and off:
        start = start + off

    dist = session.get("total_distance") or 0
    moving = session.get("total_timer_time") or 0
    tss = session.get("training_stress_score")
    return {
        "file": os.path.basename(path),
        "start": start.strftime("%Y-%m-%d %H:%M") if start else "",
        "profile": session.get("sport_profile_name") or session.get("sport") or "",
        "sport": session.get("sport"),
        "km": round(dist / 1000, 2),
        "moving_s": int(moving),
        "elapsed_s": int(session.get("total_elapsed_time") or 0),
        "avg_kmh": round((session.get("enhanced_avg_speed") or 0) * 3.6, 1),
        "max_kmh": round((session.get("enhanced_max_speed") or 0) * 3.6, 1),
        "ascent_m": session.get("total_ascent"),
        "descent_m": session.get("total_descent"),
        "avg_hr": session.get("avg_heart_rate"),
        "max_hr": session.get("max_heart_rate"),
        "avg_w": session.get("avg_power"),
        "max_w": session.get("max_power"),
        "np_w": session.get("normalized_power"),
        "ftp_w": session.get("threshold_power"),
        "tss": round(tss, 1) if tss else None,
        "if": session.get("intensity_factor"),
        "kcal": session.get("total_calories"),
        "avg_cad": session.get("avg_cadence"),
        "temp_c": session.get("avg_temperature"),
        "te_aer": session.get("total_training_effect"),
        "te_ana": session.get("total_anaerobic_training_effect"),
    }


def parse_fit_track(path: str) -> dict:
    """Every GPS trackpoint in one .fit file, for GPX export."""
    import fitdecode

    points, session, activity = [], None, None
    with fitdecode.FitReader(path) as fr:
        for frame in fr:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "session" and session is None:
                session = {f.name: f.value for f in frame.fields}
                continue
            if frame.name == "activity" and activity is None:
                activity = {f.name: f.value for f in frame.fields}
                continue
            if frame.name != "record":
                continue
            r = {f.name: f.value for f in frame.fields}
            lat, lon = r.get("position_lat"), r.get("position_long")
            if lat is None or lon is None:
                continue  # indoor/paused sample, or GPS not yet locked
            # fitdecode hands back raw semicircles for position fields.
            if abs(lat) > 180:
                lat *= SEMICIRCLE
            if abs(lon) > 180:
                lon *= SEMICIRCLE
            points.append({
                "lat": lat, "lon": lon,
                "ele": r.get("enhanced_altitude", r.get("altitude")),
                "time": r.get("timestamp"),
                "hr": r.get("heart_rate"),
                "cad": r.get("cadence"),
                "power": r.get("power"),
                "temp": r.get("temperature"),
            })
    return {"points": points, "session": session or {}, "activity": activity or {}}


def hms(sec) -> str:
    """H:MM:SS - hours keep counting past 24 (timedelta would say "1 day, ...")."""
    if not sec:
        return "-"
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def collect_rides(mirror: str) -> list[dict]:
    act = os.path.join(mirror, "Garmin", "Activities")
    if not os.path.isdir(act):
        raise SystemExit(f"no Activities folder in {mirror} - run `sync` first")
    rides = []
    for name in sorted(os.listdir(act)):
        if name.lower().endswith(".fit"):
            r = parse_fit(os.path.join(act, name))
            if r:
                rides.append(r)
    return rides


def select_rides(args) -> tuple[str, list[dict]]:
    mirror = resolve_mirror(args.mirror)
    rides = [r for r in collect_rides(mirror) if not r.get("error")]
    rides.sort(key=lambda r: r["start"])
    if getattr(args, "since", None):
        rides = [r for r in rides if r["start"][:10] >= args.since]
    if getattr(args, "until", None):
        rides = [r for r in rides if r["start"][:10] <= args.until]
    if getattr(args, "limit", None):
        rides = rides[-args.limit:]
    return mirror, rides


# -------------------------------------------------------------- exports ----

CSV_FIELDS = ["start", "profile", "sport", "km", "moving_s", "elapsed_s",
              "avg_kmh", "max_kmh", "ascent_m", "descent_m", "avg_hr", "max_hr",
              "avg_w", "max_w", "np_w", "ftp_w", "tss", "if", "kcal", "avg_cad",
              "temp_c", "te_aer", "te_ana", "file"]


def write_csv(rides: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rides:
            w.writerow(r)


def gpx_for(track: dict, name: str) -> str:
    """GPX 1.1 with Garmin TrackPointExtension - what Strava/Komoot expect."""
    pts = track["points"]
    head = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="ride-slurp" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
        'xmlns:gpxx="http://www.garmin.com/xmlschemas/GpxExtensions/v3">',
        "  <metadata>",
        f"    <name>{sax.escape(name)}</name>",
    ]
    if pts and pts[0]["time"]:
        head.append(f"    <time>{pts[0]['time']:%Y-%m-%dT%H:%M:%SZ}</time>")
    head += ["  </metadata>", "  <trk>", f"    <name>{sax.escape(name)}</name>"]
    sport = track["session"].get("sport")
    if sport:
        head.append(f"    <type>{sax.escape(str(sport))}</type>")
    head.append("    <trkseg>")

    body = []
    for p in pts:
        body.append(f'      <trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">')
        if p["ele"] is not None:
            body.append(f"        <ele>{p['ele']:.1f}</ele>")
        if p["time"] is not None:
            body.append(f"        <time>{p['time']:%Y-%m-%dT%H:%M:%SZ}</time>")
        ext = [(k, v) for k, v in (("hr", p["hr"]), ("cad", p["cad"]),
                                   ("atemp", p["temp"])) if v is not None]
        if ext or p["power"] is not None:
            body.append("        <extensions>")
            if p["power"] is not None:
                body.append(f"          <power>{int(p['power'])}</power>")
            if ext:
                body.append("          <gpxtpx:TrackPointExtension>")
                for k, v in ext:
                    body.append(f"            <gpxtpx:{k}>{v}</gpxtpx:{k}>")
                body.append("          </gpxtpx:TrackPointExtension>")
            body.append("        </extensions>")
        body.append("      </trkpt>")

    return "\n".join(head + body + ["    </trkseg>", "  </trk>", "</gpx>"]) + "\n"


def cmd_export(args) -> int:
    mirror, rides = select_rides(args)

    if args.format == "csv":
        out = args.out or "rides.csv"
        write_csv(rides, out)
        print(json.dumps({"ok": True, "format": "csv", "rides": len(rides),
                          "path": os.path.abspath(out)}))
        return 0

    if args.format == "json":
        out = args.out or "rides.json"
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rides, f, indent=1, default=str, ensure_ascii=False)
        print(json.dumps({"ok": True, "format": "json", "rides": len(rides),
                          "path": os.path.abspath(out)}))
        return 0

    # gpx: one file per ride
    out_dir = args.out or "gpx"
    os.makedirs(out_dir, exist_ok=True)
    act = os.path.join(mirror, "Garmin", "Activities")
    written, skipped = [], []
    for r in rides:
        src = os.path.join(act, r["file"])
        try:
            track = parse_fit_track(src)
        except Exception as e:  # noqa: BLE001
            skipped.append({"file": r["file"], "reason": str(e)})
            continue
        if not track["points"]:
            skipped.append({"file": r["file"], "reason": "no GPS points (indoor ride?)"})
            continue
        stamp = r["start"].replace(" ", "-").replace(":", "")
        name = f"{stamp}-{slug(str(r['profile']) or 'ride')}"
        path = os.path.join(out_dir, f"{name}.gpx")
        with open(path, "w", encoding="utf-8") as f:
            f.write(gpx_for(track, f"{r['start']} {r['profile']}".strip()))
        written.append({"path": os.path.abspath(path),
                        "points": len(track["points"]), "km": r["km"]})
        print(f"  {name}.gpx  ({len(track['points'])} points, {r['km']} km)",
              file=sys.stderr)

    print(json.dumps({"ok": True, "format": "gpx", "written": len(written),
                      "skipped": skipped, "out_dir": os.path.abspath(out_dir),
                      "files": written}))
    return 0


# ---------------------------------------------------------------- rides ----

def render_table(rides: list[dict], lang: str) -> str:
    L = LABELS[lang]
    head = (f"| {L['date']} | {L['profile']} | km | {L['moving']} | km/h | "
            f"{L['ascent']} | HR | W | NP | TSS |")
    out = [head, "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rides:
        out.append(
            f"| {r['start']} | {r['profile']} | {num(r['km'], 2, lang)} | "
            f"{hms(r['moving_s'])} | {num(r['avg_kmh'], 1, lang)} | "
            f"{num(r['ascent_m'], 0, lang)} | {num(r['avg_hr'], 0, lang)} | "
            f"{num(r['avg_w'], 0, lang)} | {num(r['np_w'], 0, lang)} | "
            f"{num(r['tss'], 0, lang)} |")
    return "\n".join(out)


def cmd_rides(args) -> int:
    _, rides = select_rides(args)

    if args.format == "json":
        print(json.dumps(rides, indent=1, default=str, ensure_ascii=False))
        return 0
    if args.format == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rides:
            w.writerow(r)
        return 0
    if args.format == "markdown":
        print(render_table(rides, args.lang))
        return 0

    hdr = (f"{'date':<16} {'profile':<12} {'km':>7} {'moving':>9} "
           f"{'km/h':>5} {'asc':>5} {'HR':>4} {'W':>4} {'NP':>4} "
           f"{'TSS':>6} {'kcal':>5}")
    print(hdr)
    print("-" * len(hdr))
    for r in rides:
        print(f"{r['start']:<16} {str(r['profile'])[:12]:<12} {r['km']:>7.2f} "
              f"{hms(r['moving_s']):>9} {r['avg_kmh']:>5.1f} "
              f"{str(r['ascent_m'] or '-'):>5} {str(r['avg_hr'] or '-'):>4} "
              f"{str(r['avg_w'] or '-'):>4} {str(r['np_w'] or '-'):>4} "
              f"{str(r['tss'] or '-'):>6} {str(r['kcal'] or '-'):>5}")
    t = totals(rides)
    print("-" * len(hdr))
    print(f"{t['n']} rides | {t['km']:.1f} km | {hms(t['moving_s'])} moving | "
          f"{t['ascent_m']:,} m ascent | {t['tss']:.0f} TSS")
    return 0


# ------------------------------------------------------------------ log ----

LABELS = {
    "en": {"title": "Cycling", "generated": "Rides from a Garmin device. Generated by "
           "`ride_slurp.py log` - do not edit by hand.",
           "month_totals": "Month totals", "rides": "Rides", "details": "Details",
           "date": "Date", "profile": "Profile", "moving": "Moving", "ascent": "Ascent",
           "distance": "Distance", "time": "Moving time", "elev": "Elevation gain",
           "cal": "Calories", "avg_dist": "Avg distance", "avg_speed": "Avg speed",
           "longest": "Longest ride", "route": "Route", "power": "Power", "body": "Body",
           "env": "Environment", "raw": "Raw file", "total": "All time",
           "months": "Months", "index_title": "Ride log", "count": "Rides",
           "prev": "Previous month", "next": "Next month",
           "index_intro": "One log per month, generated from the `.fit` files "
                          "on a Garmin device."},
    "de": {"title": "Radtraining", "generated": "Fahrten vom Garmin. Generiert mit "
           "`ride_slurp.py log` - **nicht von Hand bearbeiten**.",
           "month_totals": "Monatssumme", "rides": "Fahrten", "details": "Details",
           "date": "Datum", "profile": "Profil", "moving": "Fahrzeit", "ascent": "Hm",
           "distance": "Distanz", "time": "Fahrzeit", "elev": "Höhenmeter",
           "cal": "Kalorien", "avg_dist": "ø Distanz", "avg_speed": "ø Geschwindigkeit",
           "longest": "Längste Fahrt", "route": "Strecke", "power": "Leistung",
           "body": "Körper", "env": "Umgebung", "raw": "Rohdaten", "total": "Gesamt",
           "months": "Monate", "index_title": "Fahrtenlog", "count": "Fahrten",
           "prev": "Vormonat", "next": "Folgemonat",
           "index_intro": "Ein Log pro Monat, erzeugt aus den `.fit`-Dateien "
                          "des Garmin."},
}


def num(x, dec=0, lang="en") -> str:
    if x is None:
        return "-"
    s = f"{x:,.{dec}f}"
    if lang == "de":  # 1.234,5
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s


def totals(rides: list[dict]) -> dict:
    return {
        "n": len(rides),
        "km": sum(r["km"] for r in rides),
        "moving_s": sum(r["moving_s"] for r in rides),
        "ascent_m": sum(r["ascent_m"] or 0 for r in rides),
        "tss": sum(r["tss"] or 0 for r in rides),
        "kcal": sum(r["kcal"] or 0 for r in rides),
    }


def link(target: str, text: str, style: str) -> str:
    """Obsidian wiki-link or a plain relative markdown link."""
    return f"[[{target}]]" if style == "obsidian" else f"[{text}]({target}.md)"


def render_month(month, rides, prev, nxt, lang, style) -> str:
    L = LABELS[lang]
    t = totals(rides)
    longest = max(rides, key=lambda r: r["km"])
    hrs = t["moving_s"] / 3600 or 1
    out = [f"# {L['title']} {month}", "", L["generated"], "",
           f"## {L['month_totals']}", "", "| | |", "|---|---|",
           f"| {L['count']} | {t['n']} |",
           f"| {L['distance']} | {num(t['km'], 1, lang)} km |",
           f"| {L['time']} | {hms(t['moving_s'])} |",
           f"| {L['elev']} | {num(t['ascent_m'], 0, lang)} m |",
           f"| TSS | {num(t['tss'], 0, lang)} |",
           f"| {L['cal']} | {num(t['kcal'], 0, lang)} kcal |",
           f"| {L['avg_dist']} | {num(t['km']/t['n'], 1, lang)} km |",
           f"| {L['avg_speed']} | {num(t['km']/hrs, 1, lang)} km/h |",
           f"| {L['longest']} | {num(longest['km'], 2, lang)} km "
           f"({longest['start'][:10]}) |",
           "", f"## {L['rides']}", "", render_table(rides, lang), "",
           f"## {L['details']}", ""]

    for r in rides:
        out += [f"### {r['start']}", "",
                f"**{r['profile']} · {num(r['km'], 2, lang)} km · "
                f"{hms(r['moving_s'])} · {num(r['ascent_m'], 0, lang)} m**", "",
                f"- **{L['route']}** - {num(r['km'], 2, lang)} km · "
                f"ø {num(r['avg_kmh'], 1, lang)} km/h · "
                f"max {num(r['max_kmh'], 1, lang)} km/h · "
                f"↑{num(r['ascent_m'], 0, lang)} m ↓{num(r['descent_m'], 0, lang)} m",
                f"- **{L['power']}** - ø {num(r['avg_w'], 0, lang)} W · "
                f"max {num(r['max_w'], 0, lang)} W · NP {num(r['np_w'], 0, lang)} W · "
                f"IF {num(r['if'], 2, lang)} · TSS {num(r['tss'], 1, lang)} "
                f"(FTP {num(r['ftp_w'], 0, lang)} W)",
                f"- **{L['body']}** - ø {num(r['avg_hr'], 0, lang)} bpm · "
                f"max {num(r['max_hr'], 0, lang)} bpm · "
                f"ø {num(r['avg_cad'], 0, lang)} rpm · "
                f"{num(r['kcal'], 0, lang)} kcal",
                f"- **{L['env']}** - ø {num(r['temp_c'], 0, lang)} °C",
                f"- {L['raw']}: `Garmin/Activities/{r['file']}`", ""]

    nav = []
    if prev:
        nav.append(f"- {L['prev']}: {link(f'rides-{prev}', prev, style)}")
    if nxt:
        nav.append(f"- {L['next']}: {link(f'rides-{nxt}', nxt, style)}")
    if nav:
        out += ["## " + L["months"], ""] + nav
    return "\n".join(out) + "\n"


def render_index(by_month, lang, style) -> str:
    L = LABELS[lang]
    all_rides = [r for rs in by_month.values() for r in rs]
    t = totals(all_rides)
    out = [f"# {L['index_title']}", "", L["index_intro"], "",
           f"## {L['total']}", "",
           f"- **{L['count']}:** {t['n']}",
           f"- **{L['distance']}:** {num(t['km'], 1, lang)} km",
           f"- **{L['time']}:** {hms(t['moving_s'])}",
           f"- **{L['elev']}:** {num(t['ascent_m'], 0, lang)} m",
           f"- **TSS:** {num(t['tss'], 0, lang)}",
           f"- **{L['cal']}:** {num(t['kcal'], 0, lang)} kcal", "",
           f"## {L['months']}", "",
           f"| {L['months']} | {L['count']} | km | {L['moving']} | "
           f"{L['ascent']} | TSS |",
           "|---|---:|---:|---:|---:|---:|"]
    for m, rs in sorted(by_month.items(), reverse=True):
        mt = totals(rs)
        out.append(f"| {link(f'rides-{m}', m, style)} | {mt['n']} | "
                   f"{num(mt['km'], 1, lang)} | {hms(mt['moving_s'])} | "
                   f"{num(mt['ascent_m'], 0, lang)} | {num(mt['tss'], 0, lang)} |")
    return "\n".join(out) + "\n"


def cmd_log(args) -> int:
    _, rides = select_rides(args)
    if not rides:
        print(json.dumps({"ok": False, "error": "no rides found"}))
        return 1

    by_month: dict[str, list[dict]] = {}
    for r in rides:
        by_month.setdefault(r["start"][:7], []).append(r)

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    months = sorted(by_month)
    written = []
    for i, m in enumerate(months):
        prev = months[i - 1] if i else None
        nxt = months[i + 1] if i + 1 < len(months) else None
        path = os.path.join(out_dir, f"rides-{m}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_month(m, by_month[m], prev, nxt, args.lang, args.link_style))
        written.append(path)

    index_name = "_index.md" if args.link_style == "obsidian" else "index.md"
    index_path = os.path.join(out_dir, index_name)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(render_index(by_month, args.lang, args.link_style))
    written.append(index_path)

    print(json.dumps({"ok": True, "months": len(months), "rides": len(rides),
                      "out_dir": os.path.abspath(out_dir),
                      "index": os.path.abspath(index_path),
                      "files": [os.path.abspath(p) for p in written]}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_filters(sp):
        sp.add_argument("--mirror", help=f"device mirror (default: under {DEFAULT_ROOT})")
        sp.add_argument("--since", metavar="YYYY-MM-DD")
        sp.add_argument("--until", metavar="YYYY-MM-DD")
        sp.add_argument("--limit", type=int, help="only the N most recent rides")

    d = sub.add_parser("detect", help="is a device plugged in, and what is it")
    d.set_defaults(func=cmd_detect)

    s = sub.add_parser("sync", help="mirror new files off the device")
    s.add_argument("--mirror")
    s.add_argument("--all", action="store_true",
                   help="also pull map tiles + SQL caches (tens of GB, slow)")
    s.add_argument("--dry-run", action="store_true", help="list what would be pulled")
    s.set_defaults(func=cmd_sync)

    r = sub.add_parser("rides", help="summarize recorded activities")
    add_filters(r)
    r.add_argument("--format", choices=["table", "json", "csv", "markdown"],
                   default="table")
    r.add_argument("--lang", choices=["en", "de"], default="en")
    r.set_defaults(func=cmd_rides)

    e = sub.add_parser("export", help="write rides out as gpx / csv / json")
    add_filters(e)
    e.add_argument("--format", choices=["gpx", "csv", "json"], required=True)
    e.add_argument("--out", help="output file (csv/json) or directory (gpx)")
    e.set_defaults(func=cmd_export)

    l = sub.add_parser("log", help="markdown ride log, one file per month")
    add_filters(l)
    l.add_argument("--out-dir", required=True, help="folder for the per-month notes")
    l.add_argument("--lang", choices=["en", "de"], default="en")
    l.add_argument("--link-style", choices=["plain", "obsidian"], default="plain",
                   help="plain markdown links (default) or Obsidian [[wiki-links]]")
    l.set_defaults(func=cmd_log)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
