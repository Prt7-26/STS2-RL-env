"""Pure-function tests — Day-7 P0.

Verifies action encoding/decoding, observation tensorization, action-mask
construction, and text rendering against synthetic /observe + /action_mask
payloads. Does NOT require STS2 to be running.

Run with::

    cd sts2-gym/py
    python -m sts2_gym.test_env_pure
"""
from __future__ import annotations

import sys

import numpy as np

from sts2_gym.env import (
    ACTION_DIM,
    ENEMY_MAX,
    END_TURN_IDX,
    HAND_MAX,
    SELECTOR_CONFIRM_IDX,
    SELECTOR_MAX,
    SELECTOR_PICK_BASE,
    SELECTOR_SKIP_IDX,
    SELECTOR_UNPICK_BASE,
    build_action_mask,
    decode_action,
    encode_observation,
)
from sts2_gym.renderer import render_text, strip_bbcode

# --- synthetic fixture: 3 enemies, 4 cards in hand, mid-play_phase ---

CREATURE_PLAYER = {
    "combat_id": 0,
    "side": "Player",
    "is_player": True,
    "is_alive": True,
    "is_hittable": True,
    "current_hp": 61,
    "max_hp": 68,
    "block": 0,
    "slot_name": "PlayerSlot1",
    "character_id": "IRONCLAD",
    "powers": [],
}
CREATURE_E1 = {
    "combat_id": 10,
    "side": "Enemy",
    "is_player": False,
    "is_alive": True,
    "is_hittable": True,
    "current_hp": 40,
    "max_hp": 40,
    "block": 0,
    "slot_name": "EnemyFront",
    "monster_id": "CHOMPER_NORMAL",
    "powers": [{"id": "VULNERABLE", "amount": 2}],
    "next_move": {"id": "atk_8", "intents": [{"type": "Attack", "total_damage": 8, "repeats": 1}]},
}
CREATURE_E2 = {
    "combat_id": 11,
    "side": "Enemy",
    "is_player": False,
    "is_alive": True,
    "is_hittable": True,
    "current_hp": 30,
    "max_hp": 40,
    "block": 5,
    "slot_name": "EnemyBack",
    "monster_id": "CHOMPER_NORMAL",
    "powers": [],
    "next_move": {"id": "blk_6", "intents": [{"type": "Defend"}]},
}
CREATURE_E_DEAD = {  # corpse — should not appear in enemy_slot ordering
    "combat_id": 12,
    "side": "Enemy",
    "is_player": False,
    "is_alive": False,
    "is_hittable": False,
    "current_hp": 0,
    "max_hp": 30,
    "block": 0,
    "slot_name": "EnemySide",
    "monster_id": "CHOMPER_NORMAL",
    "powers": [],
}

HAND_CARDS = [
    {  # 0: Strike — AnyEnemy, requires target
        "id": "STRIKE_RED",
        "cost": 1,
        "canonical_cost": 1,
        "costs_x": False,
        "upgrade_level": 0,
        "is_upgraded": False,
        "is_upgradable": True,
        "target_type": "AnyEnemy",
        "can_play": True,
    },
    {  # 1: Defend — Self
        "id": "DEFEND_RED",
        "cost": 1,
        "canonical_cost": 1,
        "costs_x": False,
        "upgrade_level": 0,
        "is_upgraded": False,
        "is_upgradable": True,
        "target_type": "Self",
        "can_play": True,
    },
    {  # 2: Cleave — AllEnemies
        "id": "CLEAVE",
        "cost": 1,
        "canonical_cost": 1,
        "costs_x": False,
        "upgrade_level": 0,
        "is_upgraded": False,
        "is_upgradable": True,
        "target_type": "AllEnemies",
        "can_play": True,
    },
    {  # 3: too-expensive Strike — not playable
        "id": "STRIKE_RED",
        "cost": 99,
        "canonical_cost": 99,
        "costs_x": False,
        "upgrade_level": 0,
        "is_upgraded": False,
        "is_upgradable": True,
        "target_type": "AnyEnemy",
        "can_play": False,
    },
]

OBS_PAYLOAD = {
    "phase": "combat",
    "in_run": True,
    "snapshot_age_ms": 12,
    "partial": False,
    "combat": {
        "round": 3,
        "current_side": "Player",
        "play_phase": True,
        "encounter": "CHOMPERS_NORMAL",
        "modifier_ids": [],
        "creatures": [CREATURE_PLAYER, CREATURE_E1, CREATURE_E2, CREATURE_E_DEAD],
        "enemy_count": 3,
        "creature_count": 4,
        "hittable_enemy_count": 2,
        "players": [
            {
                "net_id": 1,
                "in_combat_state": True,
                "energy": 3,
                "max_energy": 3,
                "stars": 0,
                "hand": HAND_CARDS,
                "hand_count": 4,
                "draw_count": 5,
                "discard_count": 0,
                "exhaust_count": 0,
                "play_count": 0,
                "pets": [],
            }
        ],
        "escaped_count": 0,
    },
}

MASK_PAYLOAD = {
    "phase": "combat",
    "play_phase": True,
    "round": 3,
    "actions": [
        # card 0 Strike — 2 hittable targets
        {
            "type": "play_card",
            "card_idx": 0,
            "card_id": "STRIKE_RED",
            "cost": 1,
            "target_type": "AnyEnemy",
            "requires_target": True,
            "legal_targets": [{"combat_id": 10, "name": "CHOMPER_NORMAL"}, {"combat_id": 11, "name": "CHOMPER_NORMAL"}],
        },
        # card 1 Defend — Self, no target
        {
            "type": "play_card",
            "card_idx": 1,
            "card_id": "DEFEND_RED",
            "cost": 1,
            "target_type": "Self",
            "requires_target": False,
            "legal_targets": [],
        },
        # card 2 Cleave — AllEnemies, no target
        {
            "type": "play_card",
            "card_idx": 2,
            "card_id": "CLEAVE",
            "cost": 1,
            "target_type": "AllEnemies",
            "requires_target": False,
            "legal_targets": [],
        },
        # card 3 (too expensive) — should NOT appear in mask response, this matches mod behavior
        # End turn
        {"type": "end_turn"},
    ],
}


def assert_eq(a, b, label):
    if a != b:
        raise AssertionError(f"{label}: expected {b!r}, got {a!r}")
    print(f"  ✓ {label}")


def test_encode_observation():
    print("[test] encode_observation")
    # Day-9.3: pass no registry → idx columns default to UNKNOWN (0)
    obs = encode_observation(OBS_PAYLOAD)
    assert obs["in_combat"] == 1, obs["in_combat"]
    assert obs["round"] == 3, obs["round"]
    np.testing.assert_array_equal(obs["player"], [61, 68, 0, 3, 3, 0])
    # Enemies: dead/non-hittable filtered out, sorted by combat_id asc.
    # Day-9.3 layout: [alive, hittable, hp, max_hp, block, intent_dmg, monster_idx]
    np.testing.assert_array_equal(obs["enemies"][0], [1, 1, 40, 40, 0, 8, 0])
    np.testing.assert_array_equal(obs["enemies"][1], [1, 1, 30, 40, 5, -1, 0])
    np.testing.assert_array_equal(obs["enemies"][2], [-1, -1, -1, -1, -1, -1, -1])
    # Hand layout: [present, cost, can_play, target_type_idx, card_idx]
    np.testing.assert_array_equal(obs["hand"][0], [1, 1, 1, 2, 0])
    np.testing.assert_array_equal(obs["hand"][1], [1, 1, 1, 1, 0])
    np.testing.assert_array_equal(obs["hand"][3], [1, 99, 0, 2, 0])
    np.testing.assert_array_equal(obs["hand"][4], [-1, -1, -1, -1, -1])
    np.testing.assert_array_equal(obs["counts"], [4, 5, 0, 0, 0])
    print("  ✓ encode_observation full shape")


def test_encode_observation_with_registry():
    print("[test] encode_observation(with registry)")
    # Fake registry — just a dict-like with the methods we use.
    class FakeRegistry:
        def card_idx(self, cid):
            return {"STRIKE_RED": 7, "DEFEND_RED": 12, "CLEAVE": 33}.get(cid, 0)
        def monster_idx(self, mid):
            return {"CHOMPER_NORMAL": 4}.get(mid, 0)
    obs = encode_observation(OBS_PAYLOAD, registry=FakeRegistry())
    # Hand: STRIKE_RED → 7, DEFEND_RED → 12, CLEAVE → 33, STRIKE_RED → 7
    assert obs["hand"][0][4] == 7
    assert obs["hand"][1][4] == 12
    assert obs["hand"][2][4] == 33
    assert obs["hand"][3][4] == 7
    # Enemies: CHOMPER_NORMAL → 4 for both
    assert obs["enemies"][0][6] == 4
    assert obs["enemies"][1][6] == 4
    print("  ✓ registry encoding wires card_idx + monster_idx")


def test_build_action_mask():
    print("[test] build_action_mask")
    mask = build_action_mask(MASK_PAYLOAD, OBS_PAYLOAD["combat"])
    assert mask.shape == (ACTION_DIM,), mask.shape
    # card 0 Strike at enemy_slot 1 (E1=cid10) and 2 (E2=cid11) legal; slot 0 illegal (requires target).
    assert mask[0 * (ENEMY_MAX + 1) + 0] == False  # noqa: E712
    assert mask[0 * (ENEMY_MAX + 1) + 1] == True   # noqa: E712
    assert mask[0 * (ENEMY_MAX + 1) + 2] == True   # noqa: E712
    assert mask[0 * (ENEMY_MAX + 1) + 3] == False  # noqa: E712
    # card 1 Defend Self: only slot 0 (no-target) is legal.
    assert mask[1 * (ENEMY_MAX + 1) + 0] == True   # noqa: E712
    assert mask[1 * (ENEMY_MAX + 1) + 1] == False  # noqa: E712
    # card 2 Cleave AllEnemies: slot 0 only.
    assert mask[2 * (ENEMY_MAX + 1) + 0] == True   # noqa: E712
    # card 3 not in mask response (unplayable) → all slots False.
    for s in range(ENEMY_MAX + 1):
        assert mask[3 * (ENEMY_MAX + 1) + s] == False, f"slot {s} should be False"  # noqa: E712
    # end_turn always legal.
    assert mask[END_TURN_IDX] == True  # noqa: E712
    n_legal = int(mask.sum())
    print(f"  ✓ legal count = {n_legal} (expect 5: Strike@E1, Strike@E2, Defend, Cleave, end_turn)")
    assert n_legal == 5, n_legal


def test_decode_action_roundtrip():
    print("[test] decode_action")
    combat = OBS_PAYLOAD["combat"]
    # end_turn
    assert_eq(decode_action(END_TURN_IDX, MASK_PAYLOAD, combat), {"type": "end_turn"}, "end_turn")
    # Strike at slot 1 → enemy combat_id 10
    a = decode_action(0 * (ENEMY_MAX + 1) + 1, MASK_PAYLOAD, combat)
    assert_eq(a, {"type": "play_card", "card_idx": 0, "target_combat_id": 10}, "strike@E1")
    # Strike at slot 2 → enemy combat_id 11
    a = decode_action(0 * (ENEMY_MAX + 1) + 2, MASK_PAYLOAD, combat)
    assert_eq(a, {"type": "play_card", "card_idx": 0, "target_combat_id": 11}, "strike@E2")
    # Defend (slot 0, no target)
    a = decode_action(1 * (ENEMY_MAX + 1) + 0, MASK_PAYLOAD, combat)
    assert_eq(a, {"type": "play_card", "card_idx": 1}, "defend")


def test_mask_dead_phase():
    print("[test] build_action_mask off-turn → all False")
    mask = build_action_mask({"play_phase": False, "actions": []}, {})
    assert mask.sum() == 0
    print("  ✓ off-turn mask all-zeros")


def test_strip_bbcode():
    print("[test] strip_bbcode")
    cases = [
        ("[b]hello[/b]", "hello"),
        ("[color=red]warn[/color] then [i]italic[/i]", "warn then italic"),
        ("plain", "plain"),
        ("", ""),
        (None, ""),
    ]
    for inp, want in cases:
        got = strip_bbcode(inp)
        assert got == want, f"strip_bbcode({inp!r}) → {got!r}, want {want!r}"
    print(f"  ✓ {len(cases)} cases pass")


def test_render_text_combat():
    print("[test] render_text(combat)")
    text = render_text(OBS_PAYLOAD, MASK_PAYLOAD)
    assert "Combat — encounter=CHOMPERS_NORMAL" in text, text
    assert "round=3" in text
    assert "Player: HP 61/68" in text
    assert "Energy 3/3" in text
    # Two hittable enemies — letters A and B.
    assert "[A]" in text and "[B]" in text
    assert "CHOMPER_NORMAL" in text
    # Hand contains 4 cards.
    assert "Hand (4):" in text
    # Card 0 is playable, card 3 isn't (different glyph).
    assert "[0] ✓ STRIKE_RED" in text
    assert "[3] ✗ STRIKE_RED" in text
    # End-of-turn legality summary.
    assert "Legal actions:" in text
    print(f"  ✓ render_text emitted {len(text.splitlines())} lines")


def test_render_text_non_combat():
    print("[test] render_text(non-combat)")
    text = render_text({"phase": "map", "in_run": True}, None)
    assert "phase=map" in text
    assert "no detailed renderer" in text
    print("  ✓ map-phase placeholder OK")


# ============================ Day-8.1 selector tests ============================

SELECTOR_OBS = {
    "phase": "card_select",
    "in_run": True,
    "snapshot_age_ms": 8,
    "partial": False,
    "combat": OBS_PAYLOAD["combat"],  # combat in progress under the selector overlay
    "selector": {
        "active": True,
        "min_select": 1,
        "max_select": 1,
        "accumulator": [],
        "can_confirm": False,
        "can_skip": False,
        "options": [
            {"option_idx": 0, "card_id": "STRIKE_RED", "cost": 1, "is_upgraded": False, "upgrade_level": 0, "target_type": "AnyEnemy"},
            {"option_idx": 1, "card_id": "DEFEND_RED", "cost": 1, "is_upgraded": False, "upgrade_level": 0, "target_type": "Self"},
            {"option_idx": 2, "card_id": "CLEAVE", "cost": 1, "is_upgraded": False, "upgrade_level": 0, "target_type": "AllEnemies"},
        ],
    },
}

SELECTOR_MASK = {
    "phase": "card_select",
    "selector_active": True,
    "min_select": 1,
    "max_select": 1,
    "actions": [
        {"type": "select_pick", "option_idx": 0, "card_id": "STRIKE_RED"},
        {"type": "select_pick", "option_idx": 1, "card_id": "DEFEND_RED"},
        {"type": "select_pick", "option_idx": 2, "card_id": "CLEAVE"},
        # No confirm (accumulator empty), no skip (min=1)
    ],
}

# Variant: multi-select with one card already picked, can_confirm + can skip both negative
SELECTOR_MULTI_MASK = {
    "phase": "card_select",
    "selector_active": True,
    "min_select": 1,
    "max_select": 3,
    "actions": [
        {"type": "select_pick", "option_idx": 1},
        {"type": "select_pick", "option_idx": 2},
        {"type": "select_unpick", "option_idx": 0},
        {"type": "select_confirm"},
    ],
}

# Variant: skippable selector (min=0)
SELECTOR_SKIPPABLE_MASK = {
    "phase": "card_select",
    "selector_active": True,
    "min_select": 0,
    "max_select": 1,
    "actions": [
        {"type": "select_pick", "option_idx": 0},
        {"type": "select_skip"},
        {"type": "select_confirm"},
    ],
}


def test_build_action_mask_selector():
    print("[test] build_action_mask(selector active, single-pick)")
    mask = build_action_mask(SELECTOR_MASK, OBS_PAYLOAD["combat"])
    assert mask.shape == (ACTION_DIM,), mask.shape
    # Combat range must be all-False (play_card blocked by active selector)
    assert mask[: END_TURN_IDX + 1].sum() == 0, "combat slots should be False during selector"
    # Pick slots 0, 1, 2 legal
    assert mask[SELECTOR_PICK_BASE + 0]
    assert mask[SELECTOR_PICK_BASE + 1]
    assert mask[SELECTOR_PICK_BASE + 2]
    # Pick slot 3 not legal
    assert not mask[SELECTOR_PICK_BASE + 3]
    # Confirm/skip not legal yet
    assert not mask[SELECTOR_CONFIRM_IDX]
    assert not mask[SELECTOR_SKIP_IDX]
    assert int(mask.sum()) == 3
    print(f"  ✓ single-pick selector: 3 legal actions")


def test_build_action_mask_selector_multi():
    print("[test] build_action_mask(multi-select with confirm + unpick)")
    mask = build_action_mask(SELECTOR_MULTI_MASK, {})
    assert int(mask.sum()) == 4, mask.sum()
    assert mask[SELECTOR_PICK_BASE + 1]
    assert mask[SELECTOR_PICK_BASE + 2]
    assert mask[SELECTOR_UNPICK_BASE + 0]
    assert mask[SELECTOR_CONFIRM_IDX]
    assert not mask[SELECTOR_SKIP_IDX]
    print("  ✓ multi-select with unpick + confirm")


def test_build_action_mask_selector_skippable():
    print("[test] build_action_mask(skippable selector min=0)")
    mask = build_action_mask(SELECTOR_SKIPPABLE_MASK, {})
    assert mask[SELECTOR_PICK_BASE + 0]
    assert mask[SELECTOR_SKIP_IDX]
    assert mask[SELECTOR_CONFIRM_IDX]
    print("  ✓ skippable selector includes skip + confirm")


def test_decode_selector_actions():
    print("[test] decode_action(selector actions)")
    assert_eq(decode_action(SELECTOR_PICK_BASE + 0, {}, {}), {"type": "select_pick", "option_idx": 0}, "select_pick[0]")
    assert_eq(decode_action(SELECTOR_PICK_BASE + 7, {}, {}), {"type": "select_pick", "option_idx": 7}, "select_pick[7]")
    assert_eq(decode_action(SELECTOR_UNPICK_BASE + 3, {}, {}), {"type": "select_unpick", "option_idx": 3}, "select_unpick[3]")
    assert_eq(decode_action(SELECTOR_CONFIRM_IDX, {}, {}), {"type": "select_confirm"}, "select_confirm")
    assert_eq(decode_action(SELECTOR_SKIP_IDX, {}, {}), {"type": "select_skip"}, "select_skip")


def test_encode_obs_selector():
    print("[test] encode_observation(selector active)")
    obs = encode_observation(SELECTOR_OBS)
    np.testing.assert_array_equal(obs["selector"], [1, 1, 1, 0])  # active, min, max, acc_count
    # Day-9.3 selector_options layout: [present, cost, is_upgraded, target_type_idx, card_idx]
    np.testing.assert_array_equal(obs["selector_options"][0], [1, 1, 0, 2, 0])  # STRIKE
    np.testing.assert_array_equal(obs["selector_options"][1], [1, 1, 0, 1, 0])  # DEFEND
    np.testing.assert_array_equal(obs["selector_options"][2], [1, 1, 0, 3, 0])  # CLEAVE
    np.testing.assert_array_equal(obs["selector_options"][3], [-1, -1, -1, -1, -1])
    print("  ✓ selector obs shape + values")


def test_render_text_selector():
    print("[test] render_text(selector active)")
    text = render_text(SELECTOR_OBS, SELECTOR_MASK)
    assert "phase=card_select" in text, text
    assert "Selector" in text or "select" in text.lower(), text
    # Card ids of options should appear
    assert "STRIKE_RED" in text
    assert "CLEAVE" in text
    print(f"  ✓ selector render emitted {len(text.splitlines())} lines")


# ============================ Day-11.A action codec + json renderer ============================


def test_action_codec_combat_no_target():
    print("[test] action_codec: combat no-target")
    from sts2_gym.action_codec import to_text, from_text
    a = {"type": "end_turn"}
    assert to_text(a) == "end turn"
    np.testing.assert_equal(from_text("end turn"), {"type": "end_turn"})
    np.testing.assert_equal(from_text("END TURN"), {"type": "end_turn"})
    print("  ✓ end turn round-trip")


def test_action_codec_play_card_with_context():
    print("[test] action_codec: play_card with context resolves card_idx + target_letter")
    from sts2_gym.action_codec import to_text, from_text
    # to_text uses context to print friendly name
    a = {"type": "play_card", "card_idx": 0, "target_combat_id": 10}
    assert "STRIKE_RED" in to_text(a, context=OBS_PAYLOAD)
    assert " on A" in to_text(a, context=OBS_PAYLOAD)
    # from_text resolves "play strike on A" → card_idx=0, target_combat_id=10
    parsed = from_text("play strike on A", context=OBS_PAYLOAD)
    assert parsed == {"type": "play_card", "card_idx": 0, "target_combat_id": 10}, parsed
    print("  ✓ play_card round-trip with context")


def test_action_codec_selector():
    print("[test] action_codec: selector actions")
    from sts2_gym.action_codec import to_text, from_text
    assert to_text({"type": "select_pick", "option_idx": 2}) == "select pick 2"
    assert from_text("select pick 2") == {"type": "select_pick", "option_idx": 2}
    assert to_text({"type": "select_confirm"}) == "select confirm"
    assert from_text("select confirm") == {"type": "select_confirm"}
    assert from_text("Select Skip") == {"type": "select_skip"}
    print("  ✓ select_* round-trip")


def test_action_codec_non_combat():
    print("[test] action_codec: non-combat actions")
    from sts2_gym.action_codec import to_text, from_text
    assert to_text({"type": "choose_map_node", "col": 3, "row": 5}) == "choose map 3,5"
    assert from_text("choose map 3,5") == {"type": "choose_map_node", "col": 3, "row": 5}
    assert to_text({"type": "choose_event_option", "option_idx": 1}) == "choose option 1"
    assert from_text("choose option 1") == {"type": "choose_event_option", "option_idx": 1}
    assert from_text("leave reward") == {"type": "leave_reward_screen"}
    assert from_text("proceed") == {"type": "proceed_after_game_over"}
    # Day-10.B: shop + rest
    assert to_text({"type": "shop_buy", "entry_idx": 2}) == "shop buy 2"
    assert from_text("shop buy 2") == {"type": "shop_buy", "entry_idx": 2}
    assert from_text("shop leave") == {"type": "shop_leave"}
    assert to_text({"type": "rest_choose", "option_idx": 0}) == "rest 0"
    assert from_text("rest 0") == {"type": "rest_choose", "option_idx": 0}
    print("  ✓ map / event / reward / game_over / shop / rest round-trip")


def test_action_codec_parse_error():
    print("[test] action_codec: parse failures raise ParseError")
    from sts2_gym.action_codec import from_text, ParseError
    for bad in ("", "lol whatever", "play"):
        try:
            from_text(bad)
        except ParseError:
            continue
        raise AssertionError(f"expected ParseError for {bad!r}")
    print("  ✓ rejects empty / nonsense / partial input")


def test_render_json():
    print("[test] render_json combat shape")
    from sts2_gym.renderer import render_json
    j = render_json(OBS_PAYLOAD, MASK_PAYLOAD)
    assert j["phase"] == "combat"
    assert "combat" in j
    assert j["combat"]["encounter"] == "CHOMPERS_NORMAL"
    assert j["combat"]["round"] == 3
    assert j["combat"]["player"]["hp"] == 61
    assert len(j["combat"]["enemies"]) == 2
    assert j["combat"]["enemies"][0]["letter"] == "A"
    assert j["combat"]["enemies"][0]["hp"] == 40
    assert j["combat"]["piles"]["hand"][0]["id"] == "STRIKE_RED"
    print(f"  ✓ render_json keys: {sorted(j.keys())}")


def test_render_json_selector():
    print("[test] render_json selector shape")
    from sts2_gym.renderer import render_json
    j = render_json(SELECTOR_OBS, SELECTOR_MASK)
    assert j["selector"]["min_select"] == 1
    assert j["selector"]["options"][0]["card_id"] == "STRIKE_RED"
    print("  ✓ selector json view")


# ============================ Day-11.B LLMActionParser ============================


def test_llm_parser_canonical():
    print("[test] LLMActionParser: canonical input passes through")
    from sts2_gym.llm_parser import LLMActionParser
    p = LLMActionParser(context=OBS_PAYLOAD)
    assert p.parse("end turn") == {"type": "end_turn"}
    assert p.parse("play strike on A") == {"type": "play_card", "card_idx": 0, "target_combat_id": 10}
    print("  ✓ canonical inputs unchanged")


def test_llm_parser_prose_extraction():
    print("[test] LLMActionParser: extracts action from surrounding prose")
    from sts2_gym.llm_parser import LLMActionParser
    p = LLMActionParser(context=OBS_PAYLOAD)
    msg = ("I should weaken the front-line first since it's about to attack. "
           "play Strike on A")
    a = p.parse(msg)
    assert a == {"type": "play_card", "card_idx": 0, "target_combat_id": 10}, a
    # Multiline reasoning then action.
    msg = "Hmm, energy=3.\nLet me defend.\nplay Defend"
    a = p.parse(msg)
    assert a["type"] == "play_card" and a["card_idx"] == 1, a
    print("  ✓ prose-wrapped actions extracted")


def test_llm_parser_synonyms():
    print("[test] LLMActionParser: synonyms normalize to canonical")
    from sts2_gym.llm_parser import LLMActionParser
    p = LLMActionParser(context=OBS_PAYLOAD)
    a = p.parse("cast Strike on A")
    assert a == {"type": "play_card", "card_idx": 0, "target_combat_id": 10}, a
    assert p.parse("end my turn") == {"type": "end_turn"}
    assert p.parse("skip the rewards") == {"type": "leave_reward_screen"}
    print("  ✓ attack-with / cast / use / pass turn synonyms")


def test_llm_parser_tool_use_json():
    print("[test] LLMActionParser: tool-use JSON shape")
    from sts2_gym.llm_parser import LLMActionParser
    p = LLMActionParser(context=OBS_PAYLOAD)
    # Pure JSON
    a = p.parse('{"action": "end_turn"}')
    assert a == {"type": "end_turn"}, a
    # JSON embedded in prose
    a = p.parse('I think I should play strike. Output: {"type": "play_card", "card": "Strike", "target": "A"}')
    assert a["type"] == "play_card" and a["card_idx"] == 0 and a["target_combat_id"] == 10, a
    # Tool-use shape with card_idx directly
    a = p.parse('{"action": "play_card", "card_idx": 1}')
    assert a == {"type": "play_card", "card_idx": 1}, a
    print("  ✓ JSON parsed in 3 shapes")


def test_llm_parser_ambiguity_resolution():
    print("[test] LLMActionParser: prefers last action by default")
    from sts2_gym.llm_parser import LLMActionParser
    p = LLMActionParser(context=OBS_PAYLOAD, on_ambiguity="last")
    msg = "first I'll play defend. Actually wait, play strike on A instead"
    a = p.parse(msg)
    assert a["target_combat_id"] == 10, a  # Strike on A is the "last" action
    p_first = LLMActionParser(context=OBS_PAYLOAD, on_ambiguity="first")
    a = p_first.parse(msg)
    assert a["card_idx"] == 1, a  # Defend is the "first"
    print("  ✓ on_ambiguity='last' / 'first'")


def test_llm_parser_failure():
    print("[test] LLMActionParser: garbage → ParseError")
    from sts2_gym.llm_parser import LLMActionParser
    from sts2_gym.action_codec import ParseError
    p = LLMActionParser()
    for bad in ("", "the weather is nice today", "..."):
        try:
            p.parse(bad)
        except ParseError:
            continue
        raise AssertionError(f"expected ParseError for {bad!r}")
    print("  ✓ rejects empty / off-topic / pure-punct")


# ============================ Day-14 schemas ============================


def _read_steprunner_action_types() -> set[str]:
    """Parse mod/StepRunner.cs and pull out every action name from the
    dispatch switch (the canonical set the server accepts on /step).

    Returns the set of literal strings on the LHS of `"foo" =>` patterns within
    the StepRunner.DispatchAsync switch. Cheap to maintain because the switch
    is small and stable.
    """
    import re
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent.parent / "mod" / "StepRunner.cs"
    text = p.read_text(encoding="utf-8")
    # The dispatch switch lives between "return type switch" and its closing "};"
    m = re.search(r"return type switch\s*\{(.+?)\};", text, re.DOTALL)
    if not m:
        raise AssertionError("could not locate the type-switch in StepRunner.cs")
    block = m.group(1)
    found = set(re.findall(r'"([a-z_]+)"\s*=>', block))
    if not found:
        raise AssertionError(f"no action names found in StepRunner.cs switch block")
    return found


def test_schemas_action_types_match_steprunner():
    print("[test] schemas: ACTION_TYPE_SCHEMAS matches StepRunner.cs dispatch")
    from sts2_gym.schemas import ACTION_TYPE_SCHEMAS
    schemas_set = set(ACTION_TYPE_SCHEMAS.keys())
    server_set = _read_steprunner_action_types()
    missing_in_schema = server_set - schemas_set
    extra_in_schema = schemas_set - server_set
    assert not missing_in_schema, (
        f"action types accepted by /step but missing from schemas.py: {missing_in_schema}\n"
        "  -> add them to ACTION_TYPE_SCHEMAS so the JSON Schema and docs stay accurate"
    )
    assert not extra_in_schema, (
        f"action types in schemas.py that /step would reject: {extra_in_schema}\n"
        "  -> remove from ACTION_TYPE_SCHEMAS or add the matching mod-side handler"
    )
    print(f"  ✓ {len(schemas_set)} action types align across mod + Python")


def test_schemas_codec_coverage():
    print("[test] schemas: action_codec.to_text handles every schema'd type")
    from sts2_gym.schemas import ACTION_TYPE_SCHEMAS
    from sts2_gym.action_codec import to_text
    minimal_args = {
        "play_card": {"card_idx": 0},
        "end_turn": {},
        "select_pick": {"option_idx": 0},
        "select_unpick": {"option_idx": 0},
        "select_confirm": {},
        "select_skip": {},
        "choose_map_node": {"col": 0, "row": 0},
        "choose_event_option": {"option_idx": 0},
        "take_reward_item": {"idx": 0},
        "leave_reward_screen": {},
        "card_reward_pick": {"idx": 0},
        "relic_pick": {"idx": 0},
        "bundle_pick": {"idx": 0},
        "treasure_open": {},
        "treasure_pick": {"idx": 0},
        "treasure_leave": {},
        "shop_buy": {"entry_idx": 0},
        "shop_leave": {},
        "rest_choose": {"option_idx": 0},
        "rest_leave": {},
        "proceed_after_game_over": {},
        "noop": {},
    }
    for type_name in ACTION_TYPE_SCHEMAS:
        action = {"type": type_name, **minimal_args[type_name]}
        text = to_text(action)
        assert text and not text.startswith("<"), (
            f"action_codec.to_text returned a fallback ('{text}') for {type_name!r}; "
            "extend to_text to emit a canonical form for every schema'd action"
        )
    print(f"  ✓ to_text covers all {len(ACTION_TYPE_SCHEMAS)} action types")


def test_schemas_files_in_sync():
    print("[test] schemas: docs/schemas/*.json match source-of-truth")
    import json
    from pathlib import Path
    from sts2_gym.schemas import ALL_SCHEMAS
    schemas_dir = Path(__file__).resolve().parent.parent.parent / "docs" / "schemas"
    if not schemas_dir.exists():
        print("  ⚠ docs/schemas/ doesn't exist yet — run `python -m sts2_gym.gen_schemas`")
        return
    drift: list[str] = []
    for name, schema in ALL_SCHEMAS.items():
        path = schemas_dir / f"{name}.schema.json"
        if not path.exists():
            drift.append(f"missing {path.name}")
            continue
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        if on_disk != schema:
            drift.append(f"out-of-date {path.name}")
    assert not drift, (
        f"schemas drift detected: {drift}\n"
        "  -> run `python -m sts2_gym.gen_schemas`"
    )
    print(f"  ✓ {len(ALL_SCHEMAS)} schema files match source-of-truth")


def main():
    tests = [
        test_encode_observation,
        test_encode_observation_with_registry,
        test_build_action_mask,
        test_decode_action_roundtrip,
        test_mask_dead_phase,
        test_strip_bbcode,
        test_render_text_combat,
        test_render_text_non_combat,
        test_build_action_mask_selector,
        test_build_action_mask_selector_multi,
        test_build_action_mask_selector_skippable,
        test_decode_selector_actions,
        test_encode_obs_selector,
        test_render_text_selector,
        # Day-11.A
        test_action_codec_combat_no_target,
        test_action_codec_play_card_with_context,
        test_action_codec_selector,
        test_action_codec_non_combat,
        test_action_codec_parse_error,
        test_render_json,
        test_render_json_selector,
        # Day-11.B
        test_llm_parser_canonical,
        test_llm_parser_prose_extraction,
        test_llm_parser_synonyms,
        test_llm_parser_tool_use_json,
        test_llm_parser_ambiguity_resolution,
        test_llm_parser_failure,
        # Day-14 schema drift checks
        test_schemas_action_types_match_steprunner,
        test_schemas_codec_coverage,
        test_schemas_files_in_sync,
    ]
    for t in tests:
        t()
    print()
    print(f"[test] ✓ {len(tests)}/{len(tests)} pure-function tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
