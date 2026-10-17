#!/usr/bin/env python3
"""Peak Guard — dagrapport over het EV-API-verbruik (monitoring, geen deel van de integratie).

Haalt via ssh de JSONL-logs van Peak Guard op (custom_components/peak_guard/logs,
gewist bij elke integratie-update, dus eerst lokaal gearchiveerd), plus de
geschiedenis van de maandpiek-sensor, en schrijft per UTC-dag een rapport met de
drempels uit de evaluatie van 2026-10-17:

  - totaal calls/dag ≥ 200 (60 % van 333)                → herbekijken
  - schema set_value per dag > 100, of ≥ 6 richtingswissels
    per uur gedurende ≥ 1 uur (ping-pong schema ↔ piek)   → hold-off na piekverlaging overwegen
  - dezelfde fout > 5× binnen 30 min                      → back-off werkt niet
  - > 10 calls binnen 10 min                              → rate-limiter bekijken
  - maandpiek gestegen terwijl schema/solar kort ervoor
    de laadstroom verhoogde                               → marge bij verhogen overwegen

Gebruik:  python3 tools/ev_api_report.py [--host homeassistant] [--days 3]
          tools/ev_api_report.sh  (launchd: haalt via ssh op en roept dit script --offline aan)
Uitvoer:  ~/Library/Logs/PeakGuardMonitor/ (archive/, report-YYYY-MM-DD.md, summary.log)
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_DIR_REMOTE = "/config/custom_components/peak_guard/logs"
PEAK_SENSOR = "sensor.p1_meter_piekverbruik_huidige_maand"
OUT = Path.home() / "Library" / "Logs" / "PeakGuardMonitor"
ARCHIVE = OUT / "archive"

DAY_TOTAL_WARN = 200
SCHEDULE_SET_WARN = 100
FLIPS_PER_HOUR_WARN = 6
SAME_ERROR_WARN = 5
BURST_10MIN_WARN = 10


def ssh(host: str, cmd: str) -> str:
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, cmd],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        raise subprocess.SubprocessError(
            f"ssh {host} → exit {res.returncode}: {res.stderr.strip()[-300:]}")
    return res.stdout


def sync_logs(host: str) -> None:
    """Kopieer elke remote JSONL; een langere versie overschrijft de lokale, nooit een kortere."""
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    names = ssh(host, f"ls {LOG_DIR_REMOTE} 2>/dev/null || true").split()
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        remote = ssh(host, f"cat {LOG_DIR_REMOTE}/{name}")
        local = ARCHIVE / name
        if not local.exists() or len(remote) >= local.stat().st_size:
            local.write_text(remote)


def peak_history(host: str, start: datetime) -> list[tuple[datetime, float]]:
    url = (
        "http://supervisor/core/api/history/period/"
        f"{start.strftime('%Y-%m-%dT%H:%M:%S')}+00:00"
        f"?filter_entity_id={PEAK_SENSOR}&minimal_response&no_attributes"
    )
    try:
        raw = ssh(host, f'curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" "{url}"')
    except subprocess.SubprocessError:
        return []
    return parse_peak_history(raw)


def parse_peak_history(raw: str) -> list[tuple[datetime, float]]:
    try:
        rows = json.loads(raw)[0] if raw.strip() else []
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return []
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.get("last_changed") or r["last_updated"])
            out.append((ts, float(r["state"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_day(day: str) -> list[dict]:
    path = ARCHIVE / f"ev_api_{day}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
            r["_ts"] = datetime.fromisoformat(r["ts"])
            rows.append(r)
        except (ValueError, KeyError):
            continue
    return sorted(rows, key=lambda r: r["_ts"])


def max_in_window(times: list[datetime], seconds: float) -> int:
    best, j = 0, 0
    for i, t in enumerate(times):
        while (t - times[j]).total_seconds() >= seconds:
            j += 1
        best = max(best, i - j + 1)
    return best


def analyse(day: str, rows: list[dict], peaks: list[tuple[datetime, float]]) -> tuple[str, list[str]]:
    flags: list[str] = []
    lines = [f"# Peak Guard EV-API — {day} (UTC)", ""]
    total = len(rows)
    lines.append(f"**Totaal:** {total} calls (budget 333)")
    if total >= DAY_TOTAL_WARN:
        flags.append(f"totaal {total} ≥ {DAY_TOTAL_WARN}")

    by = collections.Counter((r.get("cascade") or "?", r["service"]) for r in rows)
    lines += ["", "| cascade | service | aantal |", "|---|---|---|"]
    lines += [f"| {c} | {s} | {n} |" for (c, s), n in by.most_common()]

    times = [r["_ts"] for r in rows]
    hour, burst = max_in_window(times, 3600), max_in_window(times, 600)
    lines += ["", f"**Piekbelasting:** max {hour}/uur, max {burst}/10 min"]
    if burst > BURST_10MIN_WARN:
        flags.append(f"{burst} calls binnen 10 min")

    # Schema-stroomaanpassingen en richtingswissels (ping-pong met de piekbeperking).
    sets = [r for r in rows if r["service"] == "number.set_value"
            and "current" in (r.get("entity") or "") and r.get("success")]
    sched = [r for r in sets if r.get("cascade") == "schedule"]
    if len(sched) > SCHEDULE_SET_WARN:
        flags.append(f"schema set_value {len(sched)} > {SCHEDULE_SET_WARN}")
    flips_per_hour: collections.Counter = collections.Counter()
    prev_val, prev_dir = None, 0
    for r in (r for r in sets if r.get("cascade") in ("schedule", "peak")):
        v = r.get("value")
        if v is None:
            continue
        if prev_val is not None and v != prev_val:
            d = 1 if v > prev_val else -1
            if prev_dir and d != prev_dir:
                flips_per_hour[r["_ts"].strftime("%H")] += 1
            prev_dir = d
        prev_val = v
    bad_hours = sorted(h for h, n in flips_per_hour.items() if n >= FLIPS_PER_HOUR_WARN)
    lines.append(
        f"**Laadstroom:** {len(sets)} aanpassingen ({len(sched)} door schema); "
        f"op/neer-wissels schema↔piek/uur max {max(flips_per_hour.values(), default=0)}"
    )
    if bad_hours:
        flags.append(f"≥ {FLIPS_PER_HOUR_WARN} op/neer-wissels per uur om {', '.join(bad_hours)}h UTC")

    # Herhaalde fouten.
    fails = [r for r in rows if not r.get("success")]
    lines.append(f"**Fouten:** {len(fails)}")
    by_err: dict[str, list[datetime]] = collections.defaultdict(list)
    for r in fails:
        by_err[(r.get("error") or "?")[:80]].append(r["_ts"])
    for err, ts in sorted(by_err.items(), key=lambda kv: -len(kv[1])):
        burst_err = max_in_window(ts, 1800)
        lines.append(f"  - {len(ts)}× `{err}` (max {burst_err} binnen 30 min)")
        if burst_err > SAME_ERROR_WARN:
            flags.append(f"fout '{err[:40]}' {burst_err}× binnen 30 min")

    # Maandpiek gestegen kort na een verhoging van de laadstroom?
    raises = [r["_ts"] for r in sets if r.get("cascade") in ("schedule", "solar")]
    day_start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    rises = []
    for (t0, v0), (t1, v1) in zip(peaks, peaks[1:]):
        if v1 > v0 and day_start <= t1 < day_start + timedelta(days=1):
            recent = [t for t in raises if timedelta(0) <= t1 - t <= timedelta(minutes=15)]
            rises.append((t1, v0, v1, len(recent)))
    if rises:
        lines.append("**Maandpiek gestegen:**")
        for t1, v0, v1, n in rises:
            lines.append(f"  - {t1:%H:%M} UTC: {v0} → {v1} ({n} verhoging(en) in de 15 min ervoor)")
            if n:
                flags.append(f"maandpiek {v0}→{v1} om {t1:%H:%M} na een stroomverhoging")
    else:
        lines.append("**Maandpiek:** niet gestegen")

    lines += ["", "**Signalen:** " + ("; ".join(flags) if flags else "geen — binnen de drempels")]
    return "\n".join(lines) + "\n", flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="homeassistant")
    ap.add_argument("--days", type=int, default=3, help="aantal afgesloten UTC-dagen te rapporteren")
    ap.add_argument("--offline", action="store_true",
                    help="niets ophalen; archive/ en peak_history.json zijn al bijgewerkt")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    try:
        if args.offline:
            peaks = parse_peak_history((OUT / "peak_history.json").read_text())
        else:
            sync_logs(args.host)
            peaks = peak_history(args.host, now - timedelta(days=args.days + 1))
    except OSError:
        peaks = []
    except subprocess.SubprocessError as err:
        with open(OUT / "summary.log", "a") as f:
            f.write(f"{now:%Y-%m-%d %H:%M} UTC  FOUT: ssh mislukt ({err})\n")
        return 1
    for back in range(args.days, 0, -1):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        report = OUT / f"report-{day}.md"
        rows = load_day(day)
        text, flags = analyse(day, rows, peaks)
        report.write_text(text)
        if back == 1:
            with open(OUT / "summary.log", "a") as f:
                f.write(f"{day}  {len(rows):4d} calls  "
                        + ("⚠ " + "; ".join(flags) if flags else "ok") + "\n")
    print((OUT / f"report-{(now - timedelta(days=1)).strftime('%Y-%m-%d')}.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
