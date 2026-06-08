# RL Card And Intent Value Contract

Status: requested bridge enhancement  
Consumer: OpenSpire Stage-1 combat RL encoder  
Validated against live STS2-Gym on port `7777`: June 8, 2026

## Purpose

The RL policy must compare legal actions before executing them. The current
bridge provides enough information to execute cards correctly, but it does not
provide the damage or block values displayed/calculated for each hand card.

Do not calculate these values in Python. They must come from STS2 APIs so card
powers, relics, enchantments, target state, and game hooks remain authoritative.

## Current Validated Behavior

Already available and trustworthy:

- `combat.creatures[*].block` is the creature's current engine block.
- `combat.creatures[*].powers` exposes power ids and amounts.
- enemy attack intents expose `total_damage` and `repeats`.
- hand cards expose id, resolved/canonical cost, upgrade state, target type,
  and `can_play`.
- `/action_mask` uses `CardModel.CanPlay` and `CardModel.CanPlayTargeting`.

Live transition evidence:

```text
Defend:
  player block: 0 -> 5

Enemy attack:
  reported total_damage: 16
  player block before attack: 5
  observed HP loss: 11

Strike:
  target HP delta: -6
```

## Current Gaps

Hand card payloads currently omit:

- base damage and block,
- enchanted damage and block,
- effective damage after powers and hooks,
- effective block after powers and hooks,
- hit/repeat count,
- target-dependent output,
- other named dynamic variables used by calculated cards.

Enemy intent damage is computed using:

```csharp
attack.GetTotalDamage(Array.Empty<Creature>(), monsterCreature)
```

This matched the tested Chompers combat, but an empty target list does not
guarantee player-target modifiers such as Vulnerable are included.

## Requested Observation Shape

Add dynamic variables to each serialized hand card:

```json
{
  "id": "STRIKE_IRONCLAD",
  "cost": 1,
  "dynamic_vars": {
    "Damage": {
      "kind": "damage",
      "base_value": 6,
      "enchanted_value": 6,
      "preview_value": 8,
      "value_props": ["Move"]
    }
  },
  "action_values": [
    {
      "target_combat_id": 4,
      "damage_per_hit": 12,
      "hit_count": 1,
      "total_damage": 12
    }
  ]
}
```

For an untargeted block card:

```json
{
  "id": "DEFEND_IRONCLAD",
  "dynamic_vars": {
    "Block": {
      "kind": "block",
      "base_value": 5,
      "enchanted_value": 5,
      "preview_value": 7,
      "value_props": ["Move"]
    }
  },
  "action_values": [
    {
      "target_combat_id": null,
      "block": 7
    }
  ]
}
```

Field names may differ, but the semantics must be explicit:

- `base_value`: card model value before enchantments and combat modifiers.
- `enchanted_value`: value after card enchantment changes.
- `preview_value`: game-computed current value after relevant global hooks.
- `action_values`: target-specific values for each currently legal action.
- `hit_count`: number of hits, separate from damage per hit.
- `total_damage`: final projected total for that target.

Keep all named dynamic variables, not only `Damage` and `Block`. Cards use
variables such as calculated damage, repeat counts, magic values, and
card-specific counters.

## Suggested Game API Path

Reverse-engineered game behavior indicates:

- `card.DynamicVars` contains named `DynamicVar` objects.
- `DynamicVar` exposes `BaseValue`, `EnchantedValue`, and `PreviewValue`.
- damage preview calculation applies the game's damage hooks.
- block preview calculation applies the game's block hooks.
- calculated damage and block variables derive state-dependent values before
  applying those hooks.

For each legal target:

1. Resolve the same legal target used by `/action_mask`.
2. Update/compute the card preview value with that target.
3. Serialize the result without mutating persistent combat state.
4. Preserve damage-per-hit and repeat count separately where available.

If calculating previews has UI or shared-state side effects, use the underlying
calculation/hook APIs or snapshot and restore preview fields.

## Enemy Intent Requirement

Calculate incoming attack damage against the actual player creature:

```text
enemy action -> actual player target -> target-aware total damage
```

The result must include target-side modifiers such as Vulnerable and any other
hooks that affect damage received. For multi-player or multi-target attacks,
return one projected value per target.

Suggested shape:

```json
{
  "type": "Attack",
  "damage_per_hit": 8,
  "repeats": 2,
  "targets": [
    {
      "combat_id": 0,
      "total_damage": 20
    }
  ]
}
```

## Acceptance Tests

The bridge enhancement is complete when these cases pass:

1. Basic Strike reports the same damage as the observed enemy HP delta.
2. Basic Defend reports the same block as the observed player block delta.
3. Strength changes projected card damage before execution.
4. Weak changes projected card damage before execution.
5. Enemy Vulnerable changes target-specific projected card damage.
6. Player Vulnerable changes target-specific enemy intent damage.
7. Dexterity-like modifiers change projected block.
8. Multi-hit cards expose damage per hit, hit count, and total damage.
9. Calculated cards update when their relevant combat state changes.
10. Reading projected values does not mutate RNG, card state, or combat state.

## Integration Ownership

Bridge-side work:

- serialize card dynamic variables with each hand card,
- enrich legal actions with target-specific projected values,
- calculate enemy intent against actual target creatures,
- add tests for the acceptance cases above.

OpenSpire-side work after the payload exists:

- update `src/combat_rl/encoding/state_encoder.py`,
- add damage/block/repeat feature slots,
- add fixtures and live assertions,
- regenerate observation schema documentation if the schema is formalized.
