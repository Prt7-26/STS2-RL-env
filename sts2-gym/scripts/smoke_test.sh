#!/bin/bash
# sts2-gym Day-1 smoke test:
#   1. dotnet build -c Release
#   2. copy sts2gym.dll + manifest to STS2 install
#   3. wait for game launch, then tail logs filtered to [sts2gym] lines
#
# Usage:
#   ./sts2-gym/scripts/smoke_test.sh
#
# Override STS2 install path:
#   STS2_INSTALL=/some/other/path ./sts2-gym/scripts/smoke_test.sh

set -euo pipefail

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

# ---------- launch prompt ----------
step "Launch STS2 now"
cat <<'EOF'
  1. Launch STS2 via Steam or Spotlight.
  2. First time only: on the MAIN MENU a popup will AUTO-appear asking
     "load mods?" — click YES. (This sets PlayerAgreedToModLoading=true
     so the mod actually loads next time.) Then quit STS2.
  3. Re-launch STS2. The mod should now actually load.
  4. Start a new run, enter a combat.
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

# ---------- tail with filter ----------
step "Tailing log — Ctrl-C to stop"
echo "  Filter: 'sts2gym | Loaded.*mod | MOD_ERROR | mods? disabled | RUNNING MODDED'"
echo "  ----------------------------------------------------------------"
echo
tail -F "$ACTIVE_LOG" | grep --line-buffered -E -i 'sts2gym|Loaded.*mod|MOD_ERROR|mods? disabled|RUNNING MODDED'
