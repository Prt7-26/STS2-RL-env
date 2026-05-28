# STS2-Gym

Gymnasium-style **RL / LLM** environment bridge for Slay the Spire 2.
The mod runs inside the game process and exposes HTTP endpoints; the Python
package wraps those endpoints in `gym.Env` and provides text / JSON observation
views, action codecs, LLM action parsers, and a starting LLM baseline.

Status: **P0 ~complete** (combat + all non-combat phases + full-run loop + save / restore + LLM baseline).
[See `STS2_GYM_DEV_PLAN.md §12`](../STS2_GYM_DEV_PLAN.md#12-实施进度事后追加2026-05) for current progress.

---

## 30-second TL;DR

```bash
# 1. Build + deploy the mod
cd /path/to/STS2env/sts2-gym
bash scripts/smoke_test.sh --no-game

# 2. Enable mod loading once (writes settings.save), then launch STS2 manually
python -m sts2_gym.install --enable-mods
open -a "Slay the Spire 2"   # macOS; or just launch via Steam

# 3. From the main menu, click "New Run" -> pick character -> enter act 1
#    (you can also do this programmatically — see "Start a fresh run" below)

# 4. Run the random agent against the live game
cd py
python -m sts2_gym.doctor                            # 6-item self-check
python -m sts2_gym.full_run_agent --verbose          # plays a full run
```

---

## Prerequisites

| | Required |
|---|---|
| STS2 install | Steam (any platform; macOS arm64 currently tested) |
| .NET SDK | ≥ 9.0 (`dotnet --version`) |
| Python | ≥ 3.10 (uses `match`/`type` syntax) |
| Dependencies | stdlib only for the core (`urllib` HTTP). `gymnasium` if you want to use `STS2CombatEnv`. `anthropic` if you want the Claude baseline |

The project depends on a local copy of `sts2.dll` + `0Harmony.dll` (located at
`../sts2-reverse/`). These are **not** redistributed — they're loaded from your
legally-owned STS2 install. See [LEGAL.md](LEGAL.md) and the gitignore.

---

## Install

```bash
# from STS2env/ root
cd sts2-gym
bash scripts/smoke_test.sh --no-game     # builds + deploys the C# mod

cd py
pip install -e .                          # installs sts2_gym Python package
python -m sts2_gym.install --enable-mods  # patches settings.save (PlayerAgreedToModLoading=true)
python -m sts2_gym.doctor                 # verify everything wires up
```

`sts2_gym.install` writes `mods_enabled = true` into Slay the Spire 2's
`settings.save` so the game loads mods on the next launch without you having to
click through the in-game consent popup ([dev plan §3.2 P0](../STS2_GYM_DEV_PLAN.md#32-进程管理器-gameprocess)).

If you'd rather give consent manually: launch STS2 once, click "Load Mods" on
the main menu popup, quit, then re-launch.

---

## Quickstart: first episode

### Option A — drive the whole run from Python

```bash
# Make sure STS2 is running and on the main menu.
cd sts2-gym/py
python -m sts2_gym.full_run_agent \
    --character IRONCLAD \
    --ascension 0 \
    --seed MYSEED \
    --verbose
```

`full_run_agent` starts a fresh run via the mod, then drives every phase
(map / event / reward / shop / rest / combat / card-select / relic-select /
bundle-select / game-over) until the run ends.

### Option B — `gym.Env` style (combat scope)

```python
import gymnasium as gym
from sts2_gym import STS2CombatEnv

env = STS2CombatEnv(
    character="IRONCLAD",         # start a fresh run on first reset
    ascension=0,
    run_seed="MYSEED",
    encounter="CHOMPERS_NORMAL",  # optional — jump to a specific encounter
    partial_obs=False,            # True hides RNG state + draw pile order
    reward_mode="sparse",         # or "shaped" (HP-delta shaping per step)
)
obs, info = env.reset()
print(info["text_obs"])           # LLM-readable view of the same state
print(info["action_mask"])        # numpy bool array of length env.action_space.n

import numpy as np
for _ in range(200):
    legal = np.flatnonzero(info["action_mask"])
    action = int(np.random.choice(legal))
    obs, reward, terminated, truncated, info = env.step(action)
    if terminated or truncated:
        break
env.close()
```

You can also access the text / JSON views without going through the env:

```python
from sts2_gym import ModBridgeClient, render_text, render_json
c = ModBridgeClient()
obs = c.observe()
print(render_text(obs))     # prose view, BBCode stripped
import json; print(json.dumps(render_json(obs), indent=2))
```

### Option C — LLM baseline

```bash
export ANTHROPIC_API_KEY=...
cd sts2-gym/py
python -m sts2_gym.examples.claude_baseline --model claude-haiku-4-5
```

~150 lines of code wrapping `LLMActionParser` and the text / JSON observation
views. Replace `claude_baseline.py` with your own loop to evaluate other LLMs.

---

## HTTP API (Mod endpoints)

The mod listens on `127.0.0.1:7777` by default (configurable via env var).

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness probe |
| `/version` | GET | Protocol + mod version |
| `/observe` | GET (`?partial=1` for PartialObs view) | Full state snapshot — phase, combat state, run state, action_mask source-of-truth |
| `/action_mask` | GET | Legal action set for the current phase |
| `/step` | POST | Apply a structured action. Body: `{"type": "play_card", "card_idx": 0, "target_combat_id": 1}` etc. |
| `/reset` | POST | Reset to a Combat-level scenario (P0 ScenarioInjector) |
| `/start_run` | POST | Begin a fresh run: `{"character": "IRONCLAD", "ascension": 0, "seed": "..."}` |
| `/save_run` | GET | Snapshot the current run as a SerializableRun JSON (between-rooms only) |
| `/restore_run` | POST | Reload a previously saved run: `{"save": {...SerializableRun JSON...}}` |
| `/selector/enable` | POST | Push our ICardSelector to drive non-combat picks. Required before agent sessions |
| `/selector/disable` | POST | Pop our ICardSelector. Restore manual play |
| `/registry` | GET | Stable card / monster / relic id → int mapping (for tensor encoding) |

Use `curl http://127.0.0.1:7777/observe | python3 -m json.tool` to inspect live state.

---

## Action space

The unified action space has 13 structured action types covering all 12 phases.
Same actions are reachable from three forms (see `sts2_gym.action_codec`):

```
Discrete int  ◄──►  Structured dict  ◄──►  Canonical text
       │                    │                       │
       └────────────┬───────┴───────┬───────────────┘
                    ▼               ▼
                  RL agent       LLM agent
```

Structured forms (subset — full list in `action_codec.py`):

```python
# Combat
{"type": "play_card", "card_idx": 2, "target_combat_id": 5}
{"type": "end_turn"}

# ICardSelector (post-combat card pick, in-combat select-to-discard, etc.)
{"type": "select_pick", "option_idx": 0}
{"type": "select_confirm"}
{"type": "select_skip"}

# Non-combat phases
{"type": "choose_map_node", "col": 2, "row": 3}
{"type": "choose_event_option", "option_idx": 1}
{"type": "take_reward_item", "idx": 0}      # claim a reward
{"type": "leave_reward_screen"}              # done with rewards
{"type": "card_reward_pick", "idx": 0}      # post-combat card choice
{"type": "relic_pick", "idx": 0}            # relic-select screen
{"type": "bundle_pick", "idx": 1}           # bundle-select screen
{"type": "shop_buy", "entry_idx": 0}
{"type": "shop_leave"}
{"type": "rest_choose", "option_idx": 0}    # REST / SMITH / DIG / MEND / etc.
{"type": "rest_leave"}                       # done at rest site
{"type": "proceed_after_game_over"}
```

Canonical text examples (consumable by `LLMActionParser`):

```
play Strike on B
end turn
pick map A2
choose option 0
buy card 0
rest
smith
upgrade Strike, Defend
```

---

## Observation views

`STS2CombatEnv.reset()` / `step()` returns the **tensor view** in `obs` and the
**text view** in `info["text_obs"]`. Both come from the same `/observe` snapshot
so wrappers can A/B compare without re-querying the game.

To get JSON / text without going through `STS2CombatEnv`:

```python
from sts2_gym import render_text, render_json, ModBridgeClient
c = ModBridgeClient()
state = c.observe()
text = render_text(state)         # human-readable prose, BBCode stripped
json_view = render_json(state)    # structured JSON for tool-use prompts
```

`partial_obs=True` (env constructor) or `client.observe(partial=True)` masks
RNG counters and the `RelicGrabBag` contents — i.e. fields a human player
can't normally see ([dev plan §2.8](../STS2_GYM_DEV_PLAN.md#28-人类可读状态渲染器-humanrendererllm-接口的核心组件)).

---

## Save / restore

`/save_run` + `/restore_run` snapshot the **between-rooms** state (full
`SerializableRun`: deck, HP, gold, potions, relics, RNG state, map, modifiers,
visited rooms). Mid-combat state is **not** captured (the game itself only saves
between rooms — multiplayer sync relies on deterministic replay, not state
checkpoints).

```python
from sts2_gym import ModBridgeClient
c = ModBridgeClient()
snap = c.save_run()             # 409 if currently mid-combat
# ... do stuff that mutates state ...
c.restore_run(snap["save"])     # reload — current run is CleanUp'd first
```

The smoke test `python -m sts2_gym.save_restore_test` round-trips a save and
asserts HP / gold / deck / ascension / act are bit-equal before and after.

---

## Throughput

| FastMode | Animation behaviour | Approx step/s |
|---|---|---|
| `Normal` | Full game animations | ~3-5 |
| `Fast` | 2× speedup | ~8-15 |
| `Instant` | All visual delays short-circuited | ≥ 50 (target) |

The mod sets `FastMode = Instant` at run start. A vanilla bug in
`NCreature.AnimDie` would NRE on the first enemy death under Instant; Day-13's
Harmony patch (`mod/Patches/NCreatureAnimDiePatch.cs`) routes around it.

---

## Determinism

Pass a `seed` string to `/start_run` (or `STS2CombatEnv(run_seed=...)`) to get
deterministic trajectories. The seed is propagated into `RunRngSet`'s 12 RNG
streams + each player's `PlayerRngSet` 3 streams (see [dev plan §2.5](../STS2_GYM_DEV_PLAN.md#25-rng-控制器-rngcontroller)).

`python -m sts2_gym.determinism_test` is a small repro check: it plays the same
seed twice and asserts the trajectories match.

---

## Debugging

Quick reference (full playbook at [`../IMPLEMENTATION_NOTES.md §5`](../IMPLEMENTATION_NOTES.md#5-调试-playbook)):

```bash
# 1. live state — what phase + how stale
curl -s http://127.0.0.1:7777/observe | python3 -m json.tool | head -40

# 2. mod log (macOS path)
grep -E "sts2gym" ~/Library/Application\ Support/SlayTheSpire2/logs/godot.log | tail -30

# 3. if HTTP hangs entirely (single-threaded listener died), force-kill
pkill -9 -f SlayTheSpire2

# 4. manual unstick if a selector is stuck
sts2-gym/scripts/unstick.sh status
```

---

## Schema (wire protocol)

The Action / Observation / SaveState / ScenarioSpec wire-protocol schemas are
codegened from [`py/sts2_gym/schemas.py`](py/sts2_gym/schemas.py) (Python
source-of-truth) into [`docs/schemas/`](docs/schemas/) as Draft 2020-12
JSON Schema:

```bash
cd sts2-gym/py
python -m sts2_gym.gen_schemas          # writes docs/schemas/*.schema.json
python -m sts2_gym.gen_schemas --check  # CI: non-zero exit on drift
```

The pure-function test suite (`python -m sts2_gym.test_env_pure`) includes a
drift check that compares the 19 action types in `schemas.py` against the
mod's actual `StepRunner.cs` dispatch switch — so renaming an action on either
side without updating the schemas trips the test.

---

## Architecture

- **Mod** ([`mod/`](mod/)) — C# class library, `[ModInitializer]` entry, runs inside the game process. HTTP listener single-threaded; all game-thread work marshalled via `GameThread.RunOnMainAsync`.
- **Python** ([`py/sts2_gym/`](py/sts2_gym/)) — `ModBridgeClient` (stdlib `urllib`), `STS2CombatEnv` (`gym.Env`), `HumanRenderer` (text + JSON), `LLMActionParser`, full-run dispatch loop.
- **Reverse-engineering reference** (`../sts2-reverse/`) — decompiled DLL is `.gitignore`-d, used only for reading game internals during development.

Detailed architecture + design decisions: [`../IMPLEMENTATION_NOTES.md`](../IMPLEMENTATION_NOTES.md).

---

## Multi-instance (VectorEnv)

STS2's `RunManager.Instance` is a per-process singleton, so each parallel env
needs its own OS process. Two recipes:

**Manual launch** — pre-launch N STS2 instances via Steam, each with a unique
`STS2GYM_PORT`:

```bash
for port in 7777 7778 7779 7780; do
    STS2GYM_PORT=$port STS2GYM_PORT_LOCKFILE=/tmp/sts2_gym_$port.port \
        open -na "Slay the Spire 2"
done
```

Then from Python:

```python
from sts2_gym import STS2VectorEnv
venv = STS2VectorEnv.from_ports([7777, 7778, 7779, 7780],
                                 character="IRONCLAD",
                                 ascension=[0, 0, 5, 10])
obs, info = venv.reset()
obs, r, term, trunc, info = venv.step(action_batch)
venv.close()
```

**Auto-spawn** (CI / batch jobs — bypasses Steam):

```python
from sts2_gym import STS2VectorEnv
venv = STS2VectorEnv.spawn(num_envs=4, base_port=7777, character="IRONCLAD")
# ... train ...
venv.close()  # also terminates each spawned STS2 process
```

Smoke test: `python -m sts2_gym.vector_smoke --ports 7777,7778 --ascensions 0,5`
verifies the two instances stay independent (A0 has no AscendersBane, A5
has 1) — this is the dev plan §6 process-isolation check.

## Caveats

- **Single process = single env.** Each parallel env needs its own STS2 process; see VectorEnv above. Dev plan reference: [§2.7](../STS2_GYM_DEV_PLAN.md#27-实例生命周期).
- **macOS path.** Mod must live at `<install>/SlayTheSpire2.app/Contents/MacOS/mods/sts2gym/`. The deploy script handles this.
- **Game updates.** STS2 is in EA; new patches may break action / model IDs. `/registry` includes `content_hash` to detect drift.
- **`decompiled_dll/` is local-only**, not pushed to any public repo (per legal redlines in [CLAUDE.md](../CLAUDE.md)).
