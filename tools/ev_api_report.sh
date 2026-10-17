#!/bin/sh
# Peak Guard — dagelijkse monitoring (launchd). De ssh-stappen gebeuren hier in de
# shell: macOS weigert lokale-netwerktoegang aan python3 wanneer launchd het start.
# Daarna analyseert ev_api_report.py enkel de lokale bestanden (--offline).
HOST="${1:-homeassistant}"
OUT="$HOME/Library/Logs/PeakGuardMonitor"
ARCHIVE="$OUT/archive"
SENSOR="sensor.p1_meter_piekverbruik_huidige_maand"
mkdir -p "$ARCHIVE"
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT

fail() {
  echo "$(date -u '+%Y-%m-%d %H:%M') UTC  FOUT: $1" >> "$OUT/summary.log"
  exit 1
}

# Eerst naar een bestand: in een pipe zou de exitcode van tar die van ssh verbergen.
ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
  'cd /config/custom_components/peak_guard/logs && tar cf - *.jsonl' > "$TMP/logs.tar" \
  || fail "logs ophalen via ssh mislukt"
tar xf "$TMP/logs.tar" -C "$TMP" || fail "logs uitpakken mislukt"
ls "$TMP"/*.jsonl >/dev/null 2>&1 || fail "geen ev_api-logs gevonden op $HOST"

# Een langere remote-versie overschrijft de lokale, nooit een kortere
# (een integratie-update wist de remote logs).
for f in "$TMP"/*.jsonl; do
  dst="$ARCHIVE/$(basename "$f")"
  if [ ! -e "$dst" ] || [ "$(wc -c < "$f")" -ge "$(wc -c < "$dst")" ]; then
    cp "$f" "$dst"
  fi
done

START=$(date -u -v-4d '+%Y-%m-%dT%H:%M:%S')
ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
  "curl -s -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" \
   'http://supervisor/core/api/history/period/${START}+00:00?filter_entity_id=${SENSOR}&minimal_response&no_attributes'" \
  > "$TMP/peak.json" 2>/dev/null
# Enkel een geldige lijst vervangt de vorige piekgeschiedenis; anders ontbreekt ze in het rapport.
if [ "$(head -c 1 "$TMP/peak.json")" = "[" ]; then
  mv "$TMP/peak.json" "$OUT/peak_history.json"
else
  rm -f "$OUT/peak_history.json"
fi

exec /usr/local/bin/python3 "$(dirname "$0")/ev_api_report.py" --offline --days 2
