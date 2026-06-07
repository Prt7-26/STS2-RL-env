# STS2-Gym Environment Bridge Handoff

This document lists what is still missing before OpenSpire can run real
rollouts and policy training against Slay the Spire 2.

The Python sampler/client/contracts now exist. The remaining critical work is
the real STS2 game-side bridge mod.

## Goal

Build a local STS2-Gym bridge mod that turns the running STS2 game into a
stepable environment:

```text
/reset(initial_combat_spec) -> observation
/observe -> observation
/action_mask -> legal actions
/step(action_id) -> observation + reward/info/done
```

Existing repo locations:

- C# bridge scaffold: `mod_src/sts2gym/`
- Python bridge contracts/client/wrapper: `src/headless_env/sts2/`
- Stage-1 sampler: `src/combat_rl/curriculum/battle_sampler.py`
- Stage-1 active combat-training home: `src/combat_rl/`
- Reverse-engineering reference only: `sts2-reverse/`

Do not implement fake combat rules in Python or C#. Legal actions and state
transitions must come from STS2 APIs.

## 1. Game-Side Reset Is Missing

Needed endpoint:

```http
POST /reset
```

Input shape:

```json
{
  "seed": 123,
  "character": "ironclad",
  "initial_combat": {
    "floor": 6,
    "deck": ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH"],
    "upgraded_cards": ["BASH"],
    "relics": ["BURNING_BLOOD"],
    "potions": [null, null, null],
    "encounter_id": "CULTIST"
  }
}
```

Missing implementation:

- Create or load a real STS2 run.
- Apply character, deck, upgraded cards, relics, potions.
- Enter or inject the requested encounter.
- Initialize combat deterministically from `seed` if possible.
- Return the first valid observation.

Likely STS2 APIs from reverse notes:

- `RunState.CreateForNewRun(...)`
- `RunManager.Instance.SetUpNewSinglePlayer(...)`
- `RunManager.Instance.EnterRoomDebug(...)`
- `CombatState(...)`
- `CombatManager.Instance.SetUpCombat(...)`
- `NGame.Instance.DebugSeedOverride = seed`

Hard requirement: reset must use STS2 construction APIs. Do not synthesize
combat behavior.

## 2. Observation Serialization Is Missing

Needed endpoint:

```http
GET /observe
```

Output shape:

```json
{
  "episode_id": "abc",
  "phase": "combat",
  "run": {},
  "combat": {},
  "is_terminal": false,
  "last_error": null
}
```

Minimum combat observation needed for RL:

- phase: `combat`, `card_reward`, `event`, `shop`, `rest`, `map`, `game_over`
- player hp, max hp, block, energy, max energy
- player powers/buffs/debuffs with ids and stack amounts
- hand cards with stable instance ids
- draw pile count
- discard pile count
- exhaust pile count
- card model id, current cost, upgraded flag, type, rarity, target type
- enemy stable combat id, monster id, hp, max hp, block
- enemy alive/targetable flags
- enemy intent/next move if visible
- enemy powers with stack amounts
- relic ids
- potion ids, slot index, usable flag, target type if available
- combat terminal won/lost state
- round number, current side, play-phase flag

Known reverse evidence:

- `RunManager.Instance.ToSave(...)` can serialize between-room run state.
- `CombatManager.Instance.DebugOnlyGetState()` exposes combat state.
- Mid-combat state does not appear to have a full built-in serializer, so the
  bridge likely needs a custom `SerializableCombatState`.

The format should leave room for a later player-visible partial observation
filter. Full-info debug observation is acceptable for the first vertical slice.

## 3. Action Mask Is Missing

Needed endpoint:

```http
GET /action_mask
```

Output shape:

```json
{
  "episode_id": "abc",
  "phase": "combat",
  "actions": [
    {
      "id": "end_turn",
      "kind": "END_TURN",
      "label": "End turn"
    },
    {
      "id": "play:card_instance_12:enemy_3",
      "kind": "PLAY_CARD_TARGETED",
      "card_id": "STRIKE_IRONCLAD",
      "card_instance_id": "card_instance_12",
      "target_id": "enemy_3",
      "label": "Strike -> Cultist"
    }
  ]
}
```

Combat action kinds needed first:

- `END_TURN`
- `PLAY_CARD_UNTARGETED`
- `PLAY_CARD_TARGETED`
- `USE_POTION_UNTARGETED`
- `USE_POTION_TARGETED`

Later non-combat kinds:

- `CARD_REWARD_CHOICE`
- `EVENT_CHOICE`
- `SHOP_CHOICE`
- `REST_CHOICE`
- `MAP_CHOICE`

Missing implementation:

- Enumerate hand cards.
- Use actual game legality:
  - `CardModel.CanPlay(out reason, out preventer)`
  - `CombatState.HittableEnemies`
  - card target type
- For targeted cards, produce one action per legal target.
- For untargeted cards, produce one action with no target.
- Always include `END_TURN` during combat play phase.
- Do not compute legality in Python.

`action.id` must be resolvable by the next `/step`. It only needs to be stable
within the current decision point.

## 4. Step Execution Is Missing

Needed endpoint:

```http
POST /step
```

Input shape:

```json
{
  "action_id": "play:card_instance_12:enemy_3"
}
```

Output shape:

```json
{
  "ok": true,
  "observation": {},
  "reward_terms": {},
  "done": false,
  "info": {}
}
```

Missing implementation:

- Resolve `action_id` back to the current card, potion, target, or end-turn
  command.
- Execute through typed STS2 APIs.
- Wait until the game reaches the next decision point.
- Return a fresh observation.

Known APIs from reverse notes:

- `CardCmd.AutoPlay(...)`
- `PlayerCmd.EndTurn(...)`
- `PotionModel.EnqueueManualUse(...)`
- `CombatManager.Instance.IsInProgress`
- `CombatManager.Instance.IsPlayPhase`
- events such as `CombatSetUp`, `TurnStarted`, `TurnEnded`,
  `PlayerActionsDisabledChanged`

Avoid UI clicking if possible. AutoSlay uses UI/polling, but training needs
typed commands and event-driven waits.

## 5. Decision-Point Waiting Is Missing

After `/step`, the bridge must not return too early.

It should wait until one of these is true:

- player combat play phase with legal actions available
- card selection/reward screen awaits choice
- map/event/shop/rest decision awaits choice
- combat ended
- run/game ended
- timeout/error

Required behavior:

```text
execute action
wait for animations/commands/state transitions
resolve current phase
return observation
```

If `/step` returns while commands are still resolving, Python rollout will see
inconsistent masks and reward deltas.

## 6. Reward Support Data Is Missing

Python can compute the Stage-1 reward if observations expose enough deltas. The
bridge may also compute `reward_terms` itself.

Locked Stage-1 reward:

```text
20 * damage_dealt_this_step / total_initial_enemy_hp
-10 * hp_lost_this_round / max_hp
terminal: win ? 100 + 50 * remaining_hp / max_hp : -100
```

Needed observation/info fields:

- total initial enemy HP at reset
- enemy HP before and after step
- player HP before and after step
- round/turn boundary flag
- terminal win/loss
- remaining player HP
- max player HP

Either bridge or Python can compute the reward, but the bridge must expose the
ground-truth data.

## 7. Stable IDs Are Missing

The bridge needs canonical ids for:

- card model id, for example `BASH`
- card instance id, for the specific copy in hand
- monster/enemy combat id
- potion slot id or index
- relic id
- encounter id
- phase id
- action id

Requirements:

- model ids should match reverse/catalog ids where possible.
- instance ids only need to be stable within the current observation/step.
- action ids must be resolvable by the next `/step`.

## 8. Error Contract Is Missing

Bridge should return structured errors, not unhandled crashes.

Suggested shape:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_ACTION",
    "message": "Action id is not legal in current phase.",
    "phase": "combat",
    "episode_id": "abc"
  }
}
```

Useful error codes:

- `BRIDGE_NOT_READY`
- `NO_ACTIVE_RUN`
- `NO_ACTIVE_COMBAT`
- `INVALID_PHASE`
- `INVALID_ACTION`
- `ACTION_NO_LONGER_LEGAL`
- `STEP_TIMEOUT`
- `RESET_FAILED`
- `OBSERVATION_FAILED`

## 9. Build And Deploy Path Is Missing

Need to confirm:

- correct STS2 managed assembly path
- `.csproj` references
- mod output name must be `sts2gym.dll`
- manifest must be `sts2gym.json`
- deploy location under local STS2 `mods/sts2gym/`
- whether `ModInitializer` can start `HttpListener` directly
- whether port `8181` is acceptable or should be configurable

Current scaffold assumes:

```text
http://127.0.0.1:8181
```

## 10. Minimal Acceptance Milestones

### Milestone A: Bridge Boots

- STS2 loads `sts2gym`.
- Log shows bridge initialized.
- `GET /observe` returns JSON, even if no active run.

### Milestone B: Observe Active Combat

- Start any normal combat manually or through debug.
- `GET /observe` returns player, hand, enemies, energy, and phase.
- `GET /action_mask` returns at least `END_TURN` and playable card actions.

### Milestone C: Step One Command

- `POST /step {"action_id":"end_turn"}`
- Game advances enemy turn and returns to next player decision or terminal.
- Fresh observation differs from previous observation.

### Milestone D: Reset To Sampled Combat

- Python creates `InitialCombatSpec`.
- `POST /reset` injects that combat.
- `/observe` matches requested deck, relics, and encounter.

### Milestone E: Random Rollout

- Python repeatedly samples from `/action_mask`.
- `/step` executes until combat terminal.
- No fake game rules are used.
- Reward terms are finite.

## 11. Questions For Collaborator

Please answer these explicitly:

- Can `CombatManager.Instance.DebugOnlyGetState()` be used in the release build,
  or do we need Harmony accessors?
- What is the safest way to inject a single combat from deck/relic/potion/
  encounter ids?
- Can we construct `CombatState` directly, or should reset always go through
  `RunManager.EnterRoomDebug(...)`?
- How do we get stable card instance ids from hand cards?
- How do we read current energy from `PlayerCombatState`?
- How do we read monster intent in a player-visible way?
- What event tells us the game has reached the next decision point after
  `CardCmd.AutoPlay`?
- Is `FastModeType.Instant` safe, or should the bridge use `Fast` first?
- Can `HttpListener` run inside the mod reliably, or should we use another IPC
  mechanism?

## Bottom Line

The missing implementation is the real game-side bridge, not the Python
sampler. The collaborator should focus on making STS2 expose and execute these
four endpoints truthfully:

```text
/reset
/observe
/action_mask
/step
```

Once those work for a single combat with `END_TURN` plus one playable targeted
card, OpenSpire can build random rollout, reward calculation, encoder, and PPO
training on top.
