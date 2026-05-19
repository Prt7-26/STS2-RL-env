#!/bin/bash
# sts2-gym smoke test:
#   1. dotnet build -c Release
#   2. copy sts2gym.dll + manifest to STS2 install
#   3. wait for game launch, then either tail logs OR probe HTTP bridge
#
# Modes (default = tail-logs):
#   ./sts2-gym/scripts/smoke_test.sh              # build + deploy + tail logs
#   ./sts2-gym/scripts/smoke_test.sh --probe      # build + deploy + curl /health /observe
#   ./sts2-gym/scripts/smoke_test.sh --no-game    # build + deploy only, skip launch prompt
#
# Override STS2 install path:
#   STS2_INSTALL=/some/other/path ./sts2-gym/scripts/smoke_test.sh
# Override mod port:
#   STS2GYM_PORT=8888 ./sts2-gym/scripts/smoke_test.sh --probe

set -euo pipefail

MODE="${1:-tail}"
case "$MODE" in
    --probe)   MODE="probe" ;;
    --no-game) MODE="no-game" ;;
    --tail|tail|"")    MODE="tail" ;;
    -h|--help)
        sed -n '2,15p' "$0"
        exit 0
        ;;
    *)
        echo "Unknown mode: $MODE  (try --probe, --tail, --no-game, --help)" >&2
        exit 2
        ;;
esac

# ---------- paths ----------
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MOD_SRC="$SCRIPT_DIR/../mod"
STS2_INSTALL="${STS2_INSTALL:-$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2}"

# IMPORTANT (macOS): ModManager.Initialize uses Godot's OS.GetExecutablePath() which on
# macOS returns the binary inside the .app bundle, so mods/ must live at
# Contents/MacOS/mods/ (next to the actual executable), NOT at the install root.
MOD_DST="$STS2_INSTALL/SlayTheSpire2.app/Contents/MacOS/mods/sts2gym"

# STS2 user-data dir (Godot writes logs here on macOS):
USER_DATA="${STS2_USER_DATA:-$HOME/Library/Application Support/SlayTheSpire2}"
SETTINGS_GLOB="$USER_DATA/steam/*/settings.save"

# ---------- pretty output ----------
red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
step()   { echo; bold "==> $*"; }
die()    { red "$*"; exit 1; }

# ---------- preflight ----------
step "Preflight"
[ -d "$STS2_INSTALL" ] || die "STS2 not installed at: $STS2_INSTALL"
[ -d "$MOD_SRC" ]      || die "mod source not found at: $MOD_SRC"
command -v dotnet >/dev/null 2>&1 || die "'dotnet' not on PATH. Install .NET SDK >= 9.0."
echo "  STS2 install: $STS2_INSTALL"
echo "  Mod source:   $MOD_SRC"
echo "  .NET SDK:     $(dotnet --version)"

# ---------- build ----------
step "Building mod (dotnet build -c Release)"
if ! (cd "$MOD_SRC" && dotnet build -c Release -v q); then
    die "Build failed. Re-run \`cd $MOD_SRC && dotnet build -c Release\` to see full errors."
fi
DLL="$MOD_SRC/bin/Release/sts2gym.dll"
[ -f "$DLL" ] || die "Build did not produce expected output: $DLL"
green "  OK -> $DLL ($(du -h "$DLL" | cut -f1))"

# ---------- deploy ----------
step "Deploying to $MOD_DST"
mkdir -p "$MOD_DST"
cp -f "$MOD_SRC/sts2gym.json" "$MOD_DST/"
cp -f "$DLL" "$MOD_DST/"
echo "  Deployed files:"
ls -la "$MOD_DST" | awk 'NR>1 {printf "    %s  %s bytes\n", $NF, $5}'

if [ "$MODE" = "no-game" ]; then
    step "Done (--no-game, skipping game launch)"
    echo "  Mod is deployed. Launch STS2 manually to verify."
    exit 0
fi

# ---------- launch prompt ----------
step "Launch STS2 now"
cat <<'EOF'
  1. Launch STS2 via Steam or Spotlight.
  2. First time only: on the MAIN MENU a popup will AUTO-appear asking
     "load mods?" — click YES. (This sets PlayerAgreedToModLoading=true
     so the mod actually loads next time.) Then quit STS2.
  3. Re-launch STS2. The mod should now actually load.
  4. For --probe mode: get past the main menu into a run (so /observe
     has real data). For --tail mode: any state works.
EOF
echo
read -rp "  Press [Enter] once STS2 is launching (or Ctrl-C to abort): " _

# ---------- find the active log file ----------
step "Searching for the active log file (poll up to ~30s)"

# Candidate dirs in order of likelihood (verified path first, then fallbacks).
LOG_DIRS=(
    "$USER_DATA/logs"
    "$HOME/Library/Application Support/Godot/app_userdata/SlayTheSpire2/logs"
    "$HOME/Library/Application Support/Godot/app_userdata"
    "$HOME/Library/Application Support/Slay the Spire 2"
    "$HOME/Library/Logs/Slay the Spire 2"
    "$HOME/Library/Logs/SlayTheSpire2"
    "$STS2_INSTALL/SlayTheSpire2.app/Contents/Logs"
)

# Picks the .log / .txt under LOG_DIRS modified within the last 5 minutes,
# with the most recent mtime.
find_newest_log() {
    for d in "${LOG_DIRS[@]}"; do
        [ -d "$d" ] || continue
        find "$d" -type f \( -name "*.log" -o -name "*.txt" \) -mmin -5 2>/dev/null
    done | while read -r f; do
        printf "%s\t%s\n" "$(stat -f '%m' "$f" 2>/dev/null)" "$f"
    done | sort -nr | head -1 | cut -f2-
}

ACTIVE_LOG=""
for _ in $(seq 1 15); do
    ACTIVE_LOG=$(find_newest_log)
    if [ -n "$ACTIVE_LOG" ] && [ -f "$ACTIVE_LOG" ]; then
        break
    fi
    printf "."
    sleep 2
done
echo

if [ -z "$ACTIVE_LOG" ]; then
    red "Could not find a recently-modified log file."
    yellow "Searched these directories:"
    for d in "${LOG_DIRS[@]}"; do
        if [ -d "$d" ]; then
            echo "    - $d  (exists, but no *.log within 5 min)"
        else
            echo "    - $d  (does not exist)"
        fi
    done
    cat <<'EOF'

Manual fallback — once you find the real log path, tail it with:

    tail -F <path> | grep -i -E 'sts2gym|Loaded.*mod|MOD_ERROR|mods? disabled|RUNNING MODDED'

Common Godot log location pattern:
    ~/Library/Application Support/Godot/app_userdata/<game_name>/logs/godot.log
EOF
    exit 1
fi

green "  Active log: $ACTIVE_LOG"

# ---------- probe mode: poll /health then dump /observe ----------
if [ "$MODE" = "probe" ]; then
    PORT="${STS2GYM_PORT:-7777}"
    URL="http://127.0.0.1:$PORT"
    step "Probing HTTP bridge at $URL (poll /health up to ~30s)"

    HEALTH_OK=0
    for _ in $(seq 1 60); do
        if curl -sS -m 1 -o /dev/null "$URL/health" 2>/dev/null; then
            HEALTH_OK=1
            break
        fi
        printf "."
        sleep 0.5
    done
    echo

    if [ "$HEALTH_OK" != "1" ]; then
        red "  /health did not respond. Verify:"
        echo "    - STS2 is running and you got past the 'load mods?' popup"
        echo "    - log shows '[sts2gym] hello' and 'Loaded 1 mods'"
        echo "    - port lockfile contents: $(cat /tmp/sts2_gym.port 2>/dev/null || echo '<missing>')"
        exit 1
    fi

    echo
    green "  ✓ /health"
    curl -sS "$URL/health" | python3 -m json.tool 2>/dev/null || curl -sS "$URL/health"
    echo
    green "  ✓ /version"
    curl -sS "$URL/version" | python3 -m json.tool 2>/dev/null || curl -sS "$URL/version"
    echo
    green "  ✓ /observe (saving full payload to /tmp/sts2gym_observe.json)"
    curl -sS "$URL/observe" -o /tmp/sts2gym_observe.json
    bytes=$(wc -c </tmp/sts2gym_observe.json | tr -d ' ')
    echo "    size: $bytes bytes"
    # Heredoc with 'PYEOF' (quoted) — bash does NOT interpolate, so Python can
    # freely use single/double quotes without escape gymnastics.
    python3 <<'PYEOF'
import json
obs = json.load(open("/tmp/sts2gym_observe.json"))
print(f"    phase            = {obs.get('phase')!r}")
print(f"    in_run           = {obs.get('in_run')}")
print(f"    snapshot_age_ms  = {obs.get('snapshot_age_ms')}")
print(f"    top-level keys   = {sorted(obs.keys())}")
run = obs.get("run") or {}
if run:
    print(f"    run.schema_version = {run.get('schema_version')}")
    print(f"    run.ascension      = {run.get('ascension')}")
    print(f"    run.game_mode      = {run.get('game_mode')!r}")
    print(f"    run.players        = {len(run.get('players') or [])}")
    print(f"    run.acts           = {len(run.get('acts') or [])}")
    rng = run.get("rng") or {}
    print(f"    run.rng.seed       = {rng.get('seed')!r}")
    print(f"    run.rng.streams    = {len(rng.get('counters') or {})}")
    players = run.get("players") or []
    if players:
        p = players[0]
        print(f"    player[0]: character={p.get('character_id')!r} "
              f"hp={p.get('current_hp')}/{p.get('max_hp')} "
              f"gold={p.get('gold')} "
              f"deck={len(p.get('deck') or [])} "
              f"relics={len(p.get('relics') or [])} "
              f"potions={len(p.get('potions') or [])}")
combat = obs.get("combat")
if combat:
    print(f"    combat.encounter  = {combat.get('encounter')!r}")
    print(f"    combat.round      = {combat.get('round')} "
          f"side={combat.get('current_side')} "
          f"play_phase={combat.get('play_phase')}")
    print(f"    combat.enemies    = {combat.get('enemy_count')} "
          f"creatures={combat.get('creature_count')}")
PYEOF
    echo
    echo "  Deeper inspection:"
    echo "    jq . /tmp/sts2gym_observe.json | less"
    echo "    cd sts2-gym/py && python3 -m sts2_gym.probe"
    exit 0
fi

# ---------- tail with filter ----------
step "Tailing log — Ctrl-C to stop"
# Filter buckets:
#   (a) Our mod's own [sts2gym] lines
#   (b) Mega Crit mod loader lifecycle messages
#   (c) Game-level ERROR / WARN / exception lines (so we surface bugs in our mod
#       or in game-vanilla code we trigger via FastMode / ScenarioInjector / etc)
#
# Note: filter is case-sensitive so 'mod' doesn't false-match 'Model', 'Modding',
# 'CardModel', etc. Per-token uppercase MOD_ERROR is matched by the explicit token.
FILTER='sts2gym|Loaded [0-9]+ mods|MOD_ERROR|mods? disabled|RUNNING MODDED|Found mod manifest|Loading assembly DLL|Calling initializer method|Finished mod initialization|^ERROR:|Caught .*[Ee]xception|\[ERROR\]|\[WARN\].*[Mm]od'
echo "  Filter (case-sensitive):"
echo "    $FILTER"
echo "  ----------------------------------------------------------------"
echo
tail -F "$ACTIVE_LOG" | grep --line-buffered -E "$FILTER"
