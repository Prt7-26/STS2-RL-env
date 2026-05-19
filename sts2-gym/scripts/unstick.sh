#!/usr/bin/env bash
# Emergency selector unstick — Day-8.1.
#
# Use when manual play stalls because our ICardSelector intercepted a
# card-selection prompt (Survivor's discard, Gambling Chip start-of-combat
# discard, Ancient One event, post-combat card reward, deck upgrade/transform,
# event "choose a card", ...). With Selector installed globally, the game UI is
# hidden — the only way to drive the choice is via /step.
#
# Usage:
#   ./unstick.sh            — show pending selector + interactive pick
#   ./unstick.sh skip       — skip if legal (min_select == 0)
#   ./unstick.sh pick 0     — pick option_idx 0 (auto-confirms if max == 1)
#   ./unstick.sh confirm    — confirm current accumulator
#   ./unstick.sh status     — show selector state, no mutation

set -u
PORT="${STS2GYM_PORT:-7777}"
BASE="http://127.0.0.1:${PORT}"

if ! curl -sf "${BASE}/health" > /dev/null; then
    echo "✗ STS2 bridge not responding on ${BASE}" >&2
    exit 1
fi

show_state() {
    curl -s "${BASE}/observe" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('selector') or {}
if not s.get('active'):
    print('No active selector. Phase:', d.get('phase'))
    sys.exit(0)
print(f'Selector active: pick {s[\"min_select\"]}..{s[\"max_select\"]}')
print(f'Accumulator: {s.get(\"accumulator\")}')
for o in (s.get('options') or []):
    print(f\"  [{o['option_idx']}] {o['card_id']}  (cost={o.get('cost')}, target={o.get('target_type')})\")
print(f'can_confirm: {s.get(\"can_confirm\")}, can_skip: {s.get(\"can_skip\")}')
"
}

post() {
    local body="$1"
    curl -s -X POST -H "Content-Type: application/json" -d "$body" "${BASE}/step" | python3 -m json.tool
}

cmd="${1:-status}"
case "$cmd" in
    status)
        show_state
        ;;
    skip)
        post '{"type":"select_skip"}'
        ;;
    confirm)
        post '{"type":"select_confirm"}'
        ;;
    pick)
        idx="${2:?usage: unstick.sh pick <option_idx>}"
        post "{\"type\":\"select_pick\",\"option_idx\":${idx}}"
        ;;
    unpick)
        idx="${2:?usage: unstick.sh unpick <option_idx>}"
        post "{\"type\":\"select_unpick\",\"option_idx\":${idx}}"
        ;;
    *)
        echo "Unknown command: $cmd" >&2
        echo "Usage: $0 [status|skip|confirm|pick <idx>|unpick <idx>]" >&2
        exit 1
        ;;
esac
