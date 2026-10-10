#!/bin/bash
# CI check on a real Mac: open Weerbot.app the way a double-click does (LaunchServices),
# from a clean first-run state, and verify the dashboard comes up with live data.
#
#   bash mac/ci_launch_test.sh <path/to/Weerbot.app> <label>
#
# Writes ci-<label>-state.json and ci-<label>-check.json in the current folder.
# Only meant for GitHub Actions (it wipes ~/Library/Application Support/Weerbot).

set -u
APP="$1"
LABEL="${2:-app}"
URL="http://127.0.0.1:8765"
DATA="$HOME/Library/Application Support/Weerbot"

ping_ok() { curl -fsS --noproxy '*' --max-time 2 "$URL/api/ping" 2>/dev/null | grep -q weerbot; }
token() { curl -fsS --noproxy '*' "$URL/" | sed -n 's/.*name="weerbot-token" content="\([^"]*\)".*/\1/p'; }
post() { curl -fsS --noproxy '*' -X POST -H "X-Weerbot-Token: $(token)" -H "Content-Type: application/json" \
              --max-time 120 "$URL$1" -d '{}'; }
die() { echo "FAIL [$LABEL]: $*"; tail -n 80 "$HOME/Library/Logs/Weerbot/"*.log 2>/dev/null; exit 1; }

echo "== [$LABEL] $APP"
[ -d "$APP" ] || die "app not found"

# clean slate: no running instance, no data folder (= a brand-new user)
if ping_ok; then post /api/shutdown >/dev/null || true; sleep 3; fi
rm -rf "$DATA" "$HOME/Library/Logs/Weerbot"

start=$(date +%s)
open -n "$APP" || die "open failed"
until ping_ok; do
    sleep 2
    [ $(( $(date +%s) - start )) -lt 480 ] || die "dashboard did not answer within 8 minutes"
done
echo "dashboard up after $(( $(date +%s) - start )) s"

# wait for live weather data and markets
ok=""
for _ in $(seq 1 60); do
    curl -fsS --noproxy '*' "$URL/api/state" -o "ci-$LABEL-state.json" || true
    if python3 - "ci-$LABEL-state.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
h = d.get("health") or {}
ev = d.get("events") or []
withobs = [e for e in ev if e.get("observed") is not None]
print(f"status={d['status']} mode={d['mode']} metar_ok={h.get('metar_ok')} markets_ok={h.get('markets_ok')} "
      f"events={len(ev)} with_obs={len(withobs)} dead={sum(e['dead'] for e in ev)} step_ms={h.get('step_ms')}")
sys.exit(0 if d["status"] == "running" and h.get("metar_ok") and len(withobs) >= max(1, len(ev) // 2) else 1)
EOF
    then ok=1; break; fi
    sleep 5
done
[ -n "$ok" ] || die "no live weather data in the dashboard"

echo "-- connection check"
post /api/check > "ci-$LABEL-check.json" || die "check endpoint failed"
python3 -c "import json;r=json.load(open('ci-$LABEL-check.json'))['result'];print('geoblock',r['geoblock'],'| markets',r['markets'],'| weather',r['weather'])"

echo "-- second launch must reuse the running instance"
open -n "$APP"; sleep 8
n=$(pgrep -f "weatherbot.app|Weerbot.app/Contents/MacOS" | wc -l | tr -d ' ')
echo "processes: $n"
if curl -fsS --noproxy '*' --max-time 2 "http://127.0.0.1:8766/api/ping" >/dev/null 2>&1; then
    die "a second instance started on port 8766"
fi

echo "-- shutdown from the dashboard"
post /api/shutdown >/dev/null || die "shutdown failed"
for _ in $(seq 1 30); do ping_ok || break; sleep 1; done
ping_ok && die "still running after shutdown"
echo "PASS [$LABEL]"
