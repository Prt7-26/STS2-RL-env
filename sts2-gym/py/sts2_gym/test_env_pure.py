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
    obs = encode_observation(OBS_PAYLOAD)
    assert obs["in_combat"] == 1, obs["in_combat"]
    assert obs["round"] == 3, obs["round"]
    np.testing.assert_array_equal(obs["player"], [61, 68, 0, 3, 3, 0])
    # Enemies: dead/non-hittable filtered out, sorted by combat_id asc.
    np.testing.assert_array_equal(
        obs["enemies"][0],
        [1, 1, 40, 40, 0, 8],  # E1
    )
    np.testing.assert_array_equal(
        obs["enemies"][1],
        [1, 1, 30, 40, 5, -1],  # E2 (defends, no attack damage)
    )
    np.testing.assert_array_equal(obs["enemies"][2], [-1, -1, -1, -1, -1, -1])
    # Hand row 0: present=1, cost=1, can_play=1, target_type=AnyEnemy (idx 2)
    np.testing.assert_array_equal(obs["hand"][0], [1, 1, 1, 2])
    # Hand row 1: Defend (Self idx=1, can_play=1)
    np.testing.assert_array_equal(obs["hand"][1], [1, 1, 1, 1])
    # Hand row 3: Strike too-expensive → can_play=0
    np.testing.assert_array_equal(obs["hand"][3], [1, 99, 0, 2])
    # Empty hand slot row 4: present=-1
    np.testing.assert_array_equal(obs["hand"][4], [-1, -1, -1, -1])
    np.testing.assert_array_equal(obs["counts"], [4, 5, 0, 0, 0])
    print("  ✓ encode_observation full shape")


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


def main():
    tests = [
        test_encode_observation,
        test_build_action_mask,
        test_decode_action_roundtrip,
        test_mask_dead_phase,
        test_strip_bbcode,
        test_render_text_combat,
        test_render_text_non_combat,
    ]
    for t in tests:
        t()
    print()
    print(f"[test] ✓ {len(tests)}/{len(tests)} pure-function tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
