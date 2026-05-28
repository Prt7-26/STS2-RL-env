"""Day-14: canonical JSON Schema source-of-truth for the STS2-Gym wire protocol.

Dev plan §5.2 mandates code-generated schemas — four documents:

    ScenarioSpec / Observation / Action / SaveState

This module defines each as a Python dict (Draft 2020-12 JSON Schema) so we
have **one** authoritative definition. The CLI :mod:`sts2_gym.gen_schemas`
serializes them to ``docs/schemas/*.schema.json`` files. Both the runtime
``LLMActionParser`` and the docs site can consume the same source.

Coverage:

* **Action** — 19 structured action types accepted by ``StepRunner.DispatchAsync``
  (mod/StepRunner.cs:91-117). This is the **strongest** schema in the suite
  because the action set is fully discrete and stable.
* **Observation** — top-level shape of ``/observe`` output. Sub-objects (combat,
  selector, map, event, reward, shop, rest, etc.) are sketched but not fully
  recursive — those views evolve fast with the game, so we describe shape +
  required keys rather than every field.
* **SaveState** — opaque ``SerializableRun`` JSON. We don't reproduce the full
  game schema (~30 nested types); we just describe the envelope that ``/save_run``
  returns and ``/restore_run`` accepts.
* **ScenarioSpec** — stub. The full Combat-level / Floor-level / Run-level
  injector envisioned in dev plan §2.2 isn't implemented yet. We schema the
  ``/start_run`` body, which is the current concrete equivalent.

If you change the wire protocol, update this file **first** and regenerate the
docs/schemas/ outputs with::

    python -m sts2_gym.gen_schemas

Schema validator: any Draft 2020-12 compliant library (``jsonschema>=4.18``).
"""
from __future__ import annotations

from typing import Any


SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


# ---------------------------------------------------------------------------
# Action — 19 types accepted by POST /step
# ---------------------------------------------------------------------------

# Per-type body schemas (excluding the "type" discriminator which is added in
# the oneOf wrapper below). Keep this list in sync with
# mod/StepRunner.cs:91-117 — there is a unit test that round-trips the names.

ACTION_TYPE_SCHEMAS: dict[str, dict[str, Any]] = {
    "play_card": {
        "description": "Play a card from the player's hand. Combat phase only.",
        "properties": {
            "type": {"const": "play_card"},
            "card_idx": {"type": "integer", "minimum": 0, "description": "0-based index into combat.players[0].hand"},
            "target_combat_id": {"type": "integer", "minimum": 0, "description": "Optional. Required for single-target cards; ignored for AoE / self-target. Match against combat.creatures[*].combat_id."},
            "card_id": {"type": "string", "description": "Advisory; ignored by the mod. Use for client-side logging only."},
            "cost": {"type": "integer", "description": "Advisory; ignored by the mod."},
        },
        "required": ["type", "card_idx"],
        "additionalProperties": False,
    },
    "end_turn": {
        "description": "End the player's turn. Combat phase only.",
        "properties": {"type": {"const": "end_turn"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "select_pick": {
        "description": "Pick option_idx in an ICardSelector-driven sub-screen (card reward, deck upgrade, select-to-discard, etc.).",
        "properties": {
            "type": {"const": "select_pick"},
            "option_idx": {"type": "integer", "minimum": 0, "description": "0-based index into selector.options"},
        },
        "required": ["type", "option_idx"],
        "additionalProperties": False,
    },
    "select_unpick": {
        "description": "Remove a previously-picked option from the selector accumulator. Only valid mid-multi-select.",
        "properties": {
            "type": {"const": "select_unpick"},
            "option_idx": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "option_idx"],
        "additionalProperties": False,
    },
    "select_confirm": {
        "description": "Submit the current ICardSelector accumulator. Valid when len(accumulator) is in [min_select, max_select].",
        "properties": {"type": {"const": "select_confirm"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "select_skip": {
        "description": "Skip the current ICardSelector request. Only valid when min_select == 0.",
        "properties": {"type": {"const": "select_skip"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "choose_map_node": {
        "description": "Pick the next map node by (col, row). Must be reachable from the current node.",
        "properties": {
            "type": {"const": "choose_map_node"},
            "col": {"type": "integer", "minimum": 0},
            "row": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "col", "row"],
        "additionalProperties": False,
    },
    "choose_event_option": {
        "description": "Pick option_idx in the current event room. When event.is_finished, this clicks the synthetic PROCEED button.",
        "properties": {
            "type": {"const": "choose_event_option"},
            "option_idx": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "option_idx"],
        "additionalProperties": False,
    },
    "take_reward_item": {
        "description": "Claim one reward item (gold, potion, relic, card_reward). For card rewards, immediately opens the card-reward sub-screen.",
        "properties": {
            "type": {"const": "take_reward_item"},
            "idx": {"type": "integer", "minimum": 0, "description": "0-based index into reward.items"},
        },
        "required": ["type", "idx"],
        "additionalProperties": False,
    },
    "leave_reward_screen": {
        "description": "Close the post-combat reward screen and proceed (either to map or to nested reward — handler picks the right exit).",
        "properties": {"type": {"const": "leave_reward_screen"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "card_reward_pick": {
        "description": "Pick one card from the card-reward sub-screen (or skip via select_skip in some configurations).",
        "properties": {
            "type": {"const": "card_reward_pick"},
            "idx": {"type": "integer", "minimum": 0, "description": "0-based index into card_reward_select.cards"},
        },
        "required": ["type", "idx"],
        "additionalProperties": False,
    },
    "relic_pick": {
        "description": "Pick one relic from a relic-select sub-screen (Neow PRECARIOUS_SHEARS, treasure rooms, etc.).",
        "properties": {
            "type": {"const": "relic_pick"},
            "idx": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "idx"],
        "additionalProperties": False,
    },
    "bundle_pick": {
        "description": "Pick one bundle from a NChooseABundleSelectionScreen (e.g. event 'choose a bundle' outcomes).",
        "properties": {
            "type": {"const": "bundle_pick"},
            "idx": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "idx"],
        "additionalProperties": False,
    },
    "treasure_open": {
        "description": "Click the chest in a treasure room. No-op if already open. Per AutoSlay TreasureRoomHandler: first step in treasure-room flow.",
        "properties": {"type": {"const": "treasure_open"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "treasure_pick": {
        "description": "Click a NTreasureRoomRelicHolder by idx (chest must be open first).",
        "properties": {
            "type": {"const": "treasure_pick"},
            "idx": {"type": "integer", "minimum": 0, "description": "0-based index into treasure.relics"},
        },
        "required": ["type", "idx"],
        "additionalProperties": False,
    },
    "treasure_leave": {
        "description": "Click the proceed button to leave the treasure room.",
        "properties": {"type": {"const": "treasure_leave"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "shop_buy": {
        "description": "Buy item entry_idx from the merchant. Flat-indexed across CardEntries + RelicEntries + PotionEntries + CardRemovalEntry.",
        "properties": {
            "type": {"const": "shop_buy"},
            "entry_idx": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "entry_idx"],
        "additionalProperties": False,
    },
    "shop_leave": {
        "description": "Leave the merchant.",
        "properties": {"type": {"const": "shop_leave"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "rest_choose": {
        "description": "Pick a rest-site option (REST / SMITH / DIG / MEND / etc.) by option_idx. Some options chain into select_pick sub-screens (e.g. SMITH).",
        "properties": {
            "type": {"const": "rest_choose"},
            "option_idx": {"type": "integer", "minimum": 0},
        },
        "required": ["type", "option_idx"],
        "additionalProperties": False,
    },
    "rest_leave": {
        "description": "Click the rest-site PROCEED button after an option resolves.",
        "properties": {"type": {"const": "rest_leave"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "proceed_after_game_over": {
        "description": "Two-stage click in the game-over screen: continue button -> return to main menu button.",
        "properties": {"type": {"const": "proceed_after_game_over"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "noop": {
        "description": "No-op. Useful for synchronization probes; returns ok=true immediately.",
        "properties": {"type": {"const": "noop"}},
        "required": ["type"],
        "additionalProperties": False,
    },
}


ACTION_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DIALECT,
    "$id": "https://sts2-gym.local/schemas/action.schema.json",
    "title": "STS2-Gym Action",
    "description": (
        "Structured-dict form of an STS2-Gym action, as accepted by POST /step.\n"
        "Round-trips to canonical text (via sts2_gym.action_codec.to_text/from_text) "
        "and to Discrete action ids (via sts2_gym.env.decode_action / build_action_mask).\n"
        "See dev plan §3.4."
    ),
    "type": "object",
    "oneOf": [
        {"type": "object", **schema}
        for schema in ACTION_TYPE_SCHEMAS.values()
    ],
}


# ---------------------------------------------------------------------------
# Observation — shape returned by GET /observe
# ---------------------------------------------------------------------------

# Per-phase sub-objects. Inside a phase the shape is well-defined, but the
# *presence* of each sub-object depends on the current phase. observation.phase
# is the discriminator; the rest is optional.

_HP_PAIR = {
    "type": "object",
    "description": "{current, max} HP pair.",
    "properties": {
        "current_hp": {"type": "integer"},
        "max_hp": {"type": "integer"},
    },
    "required": ["current_hp", "max_hp"],
}

_PHASE_ENUM = [
    "main_menu",
    "combat",
    "combat_pending",
    "card_select",
    "card_reward_select",
    "relic_select",
    "bundle_select",
    "treasure",
    "map",
    "event",
    "reward",
    "shop",
    "rest",
    "game_over",
    "between_rooms",
]

OBSERVATION_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DIALECT,
    "$id": "https://sts2-gym.local/schemas/observation.schema.json",
    "title": "STS2-Gym Observation",
    "description": (
        "Top-level shape of the JSON returned by GET /observe. Sub-objects "
        "(combat, selector, map, event, reward, shop, rest, etc.) appear only "
        "when relevant to the current phase. RNG state under run.rng.counters "
        "is hidden when ?partial=1."
    ),
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": _PHASE_ENUM,
            "description": "Coarse-grained game state discriminator. See dev plan §3.1.",
        },
        "in_run": {"type": "boolean", "description": "False at main menu."},
        "snapshot_age_ms": {"type": "integer", "description": "Milliseconds since the cached snapshot was last refreshed by a subscribed game event."},
        "partial": {"type": "boolean", "description": "True if the response was fetched with ?partial=1 (RNG / RelicGrabBag masked)."},

        "combat": {
            "type": ["object", "null"],
            "description": "Present when phase ∈ {combat, card_select, combat_pending}. See mod/CombatSnapshot.cs for fields.",
            "properties": {
                "round": {"type": "integer"},
                "play_phase": {"type": "boolean"},
                "encounter": {"type": "string"},
                "creatures": {"type": "array"},
                "players": {"type": "array"},
                "hittable_enemy_count": {"type": "integer"},
            },
        },
        "selector": {
            "type": ["object", "null"],
            "description": "Present when an ICardSelector request is open (Day-8.1).",
            "properties": {
                "active": {"type": "boolean"},
                "options": {"type": "array"},
                "accumulator": {"type": "array"},
                "min_select": {"type": "integer"},
                "max_select": {"type": "integer"},
                "context": {"type": ["string", "null"]},
            },
        },
        "map": {
            "type": ["object", "null"],
            "description": "Present when phase == 'map'.",
            "properties": {
                "current": {"type": ["object", "null"]},
                "reachable": {"type": "array"},
            },
        },
        "event": {
            "type": ["object", "null"],
            "description": "Present when phase == 'event'.",
            "properties": {
                "id": {"type": "string"},
                "options": {"type": "array"},
                "is_finished": {"type": "boolean"},
            },
        },
        "reward": {
            "type": ["object", "null"],
            "description": "Present when phase == 'reward' (post-combat NRewardsScreen).",
            "properties": {
                "items": {"type": "array"},
            },
        },
        "card_reward_select": {
            "type": ["object", "null"],
            "description": "Present when phase == 'card_reward_select' (NCardRewardSelectionScreen).",
            "properties": {
                "cards": {"type": "array"},
            },
        },
        "relic_select": {
            "type": ["object", "null"],
            "description": "Present when phase == 'relic_select'.",
            "properties": {"items": {"type": "array"}},
        },
        "bundle_select": {
            "type": ["object", "null"],
            "description": "Present when phase == 'bundle_select'.",
            "properties": {"bundles": {"type": "array"}},
        },
        "treasure": {
            "type": ["object", "null"],
            "description": "Present when phase == 'treasure' (NTreasureRoom — Day-14).",
            "properties": {
                "chest_open": {"type": "boolean"},
                "can_proceed": {"type": "boolean"},
                "relics": {"type": "array"},
            },
        },
        "shop": {
            "type": ["object", "null"],
            "description": "Present when phase == 'shop'.",
            "properties": {
                "items": {"type": "array"},
                "player_gold": {"type": "integer"},
            },
        },
        "rest": {
            "type": ["object", "null"],
            "description": "Present when phase == 'rest'.",
            "properties": {"options": {"type": "array"}},
        },
        "game_over": {
            "type": ["object", "null"],
            "description": "Present when phase == 'game_over'.",
            "properties": {"can_proceed": {"type": "boolean"}},
        },

        "run": {
            "type": ["object", "null"],
            "description": (
                "Full SerializableRun JSON for the current run (dev plan §2.1 path a). "
                "Same shape produced by the game's own save system. Includes deck, HP, "
                "gold, potions, relics, RNG state, map, modifiers, visited rooms. "
                "Under ?partial=1, rng.counters + shared_relic_grab_bag.pool are masked."
            ),
            "properties": {
                "schema_version": {"type": "integer"},
                "ascension": {"type": "integer", "minimum": 0, "maximum": 10},
                "current_act_index": {"type": "integer"},
                "players": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "character_id": {"type": ["string", "object"]},
                            "current_hp": {"type": "integer"},
                            "max_hp": {"type": "integer"},
                            "max_energy": {"type": "integer"},
                            "max_potion_slot_count": {"type": "integer"},
                            "gold": {"type": "integer"},
                            "deck": {"type": "array"},
                            "relics": {"type": "array"},
                            "potions": {"type": "array"},
                        },
                        "required": ["current_hp", "max_hp", "deck"],
                    },
                },
            },
        },
    },
    "required": ["phase", "in_run", "snapshot_age_ms"],
}


# ---------------------------------------------------------------------------
# SaveState — envelope of GET /save_run / POST /restore_run
# ---------------------------------------------------------------------------

SAVESTATE_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DIALECT,
    "$id": "https://sts2-gym.local/schemas/save_state.schema.json",
    "title": "STS2-Gym SaveState envelope",
    "description": (
        "Response shape of GET /save_run / accepted body of POST /restore_run. "
        "The `save` field is an opaque SerializableRun JSON document defined by "
        "the game's own save system — we don't re-spec its 30+ nested types. "
        "Game source: MegaCrit.Sts2.Core.Saves.SerializableRun."
    ),
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "schema_version": {"type": "integer"},
        "ascension": {"type": "integer"},
        "current_act_index": {"type": "integer"},
        "rng_streams": {"type": "integer"},
        "deck_size": {"type": "integer"},
        "hp": {"type": "integer"},
        "save": {
            "type": "object",
            "description": "Opaque SerializableRun. Round-trip via /restore_run.",
        },
    },
    "required": ["ok", "save"],
}


# ---------------------------------------------------------------------------
# ScenarioSpec — currently equivalent to /start_run body
# ---------------------------------------------------------------------------

SCENARIOSPEC_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DIALECT,
    "$id": "https://sts2-gym.local/schemas/scenario_spec.schema.json",
    "title": "STS2-Gym ScenarioSpec (Run-level)",
    "description": (
        "Currently a stub matching POST /start_run. The Combat-level / Floor-level "
        "injection layers planned in dev plan §2.2 are not yet implemented."
    ),
    "type": "object",
    "properties": {
        "character": {
            "type": "string",
            "enum": ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"],
            "description": "Case-insensitive on the wire; this schema lists the canonical UPPER form.",
        },
        "ascension": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "default": 0,
            "description": "0..10. A1+ Swarming Elites, A4+ Tight Belt (−1 potion slot), A5+ AscendersBane in deck, etc. See dev plan §3.6.",
        },
        "seed": {
            "type": "string",
            "description": "Free-form seed string. If omitted, server generates 'GYM<UTC-ticks>'. Same seed + character ⇒ identical RNG streams.",
        },
    },
    "required": ["character"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Index — what gen_schemas writes out
# ---------------------------------------------------------------------------

ALL_SCHEMAS: dict[str, dict[str, Any]] = {
    "action": ACTION_SCHEMA,
    "observation": OBSERVATION_SCHEMA,
    "save_state": SAVESTATE_SCHEMA,
    "scenario_spec": SCENARIOSPEC_SCHEMA,
}


def list_action_types() -> list[str]:
    """Convenience: return the canonical list of action type strings."""
    return list(ACTION_TYPE_SCHEMAS.keys())
