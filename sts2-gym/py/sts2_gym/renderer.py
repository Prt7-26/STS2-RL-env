"""HumanRenderer — Day-7 P0: render /observe payload as LLM-readable text.

Produces a compact, prompt-friendly representation that an LLM agent can
consume directly. Intentionally not pretty-print JSON: LLMs do better with
prose-ish structure than with deep nesting.

Pairs with [[env.STS2CombatEnv]]: the env attaches ``info["text_obs"]``
on every step/reset and exposes ``render()`` returning the same string.

Future scope (P1):
  - LocManager integration (mod-side) to resolve card/monster id → English name
    + flavor text. Currently shows raw ids (CHOMPER_NORMAL_0 etc.).
  - JSON twin view (``render_json``) for tool-use LLMs.
  - Non-combat phases (map/event/shop/...) once those land on the bridge.
"""
from __future__ import annotations

import re
from typing import Any

# Game uses Godot BBCode in some localized strings ([b], [i], [color=red], etc.).
# Strip defensively even though current Day-7 obs fields don't include localized
# text — once LocManager joins, this guard catches them automatically.
_BBCODE_RE = re.compile(r"\[/?[a-zA-Z][^\[\]]*\]")


def strip_bbcode(s: str | None) -> str:
    if not s:
        return ""
    return _BBCODE_RE.sub("", s)


def _format_card(idx: int, card: dict[str, Any]) -> str:
    name = strip_bbcode(card.get("id") or "?")
    cost = card.get("cost")
    cost_str = f"cost {cost}" if cost is not None else "cost ?"
    if card.get("costs_x"):
        cost_str = "cost X"
    flags = []
    if card.get("is_upgraded"):
        flags.append("+")
    tgt = card.get("target_type")
    if tgt and tgt not in ("None", "Self"):
        flags.append(f"target={tgt}")
    playable = "✓" if card.get("can_play") else "✗"
    suffix = f" [{' '.join(flags)}]" if flags else ""
    return f"  [{idx}] {playable} {name} ({cost_str}){suffix}"


def _format_intent(intent: dict[str, Any]) -> str:
    t = intent.get("type", "?")
    if t == "Attack":
        dmg = intent.get("total_damage")
        rep = intent.get("repeats", 1) or 1
        if dmg is not None and dmg >= 0:
            return f"Attack {dmg}×{rep}" if rep > 1 else f"Attack {dmg}"
        return "Attack ?"
    return t


def _format_enemy(letter: str, c: dict[str, Any]) -> str:
    name = strip_bbcode(c.get("monster_id") or "?")
    hp = c.get("current_hp")
    max_hp = c.get("max_hp")
    block = c.get("block") or 0
    bits = [f"HP {hp}/{max_hp}"]
    if block:
        bits.append(f"Block {block}")
    powers = c.get("powers") or []
    if powers:
        bits.append("Powers: " + ", ".join(f"{p['id']}({p['amount']})" for p in powers))
    nm = c.get("next_move") or {}
    intents = nm.get("intents") or []
    if intents:
        bits.append("Intent: " + " + ".join(_format_intent(i) for i in intents))
    return f"  [{letter}] cid={c.get('combat_id')} {name}: " + " | ".join(bits)


def _format_player_creature(c: dict[str, Any]) -> str:
    hp = c.get("current_hp")
    max_hp = c.get("max_hp")
    block = c.get("block") or 0
    powers = c.get("powers") or []
    bits = [f"HP {hp}/{max_hp}"]
    if block:
        bits.append(f"Block {block}")
    if powers:
        bits.append("Powers: " + ", ".join(f"{p['id']}({p['amount']})" for p in powers))
    return " | ".join(bits)


def render_combat(obs_payload: dict[str, Any], mask_payload: dict[str, Any] | None = None) -> str:
    """Render mid-combat state as a single text block."""
    combat = obs_payload.get("combat") or {}
    if not combat:
        return f"Not in combat (phase={obs_payload.get('phase')!r})"

    lines: list[str] = []
    enc = combat.get("encounter") or "?"
    rnd = combat.get("round", "?")
    side = combat.get("current_side", "?")
    play_phase = combat.get("play_phase")
    play_str = "your turn" if play_phase else f"side={side}"
    lines.append(f"Combat — encounter={enc} | round={rnd} | {play_str}")

    # Player creature(s) — usually one in current game, but loop is safe.
    player_creatures = [c for c in combat.get("creatures") or [] if c.get("is_player")]
    for pc in player_creatures:
        lines.append(f"Player: {_format_player_creature(pc)}")

    # Player combat state (energy / stars / piles).
    players = combat.get("players") or []
    if players:
        p0 = players[0]
        lines.append(
            f"  Energy {p0.get('energy')}/{p0.get('max_energy')} | "
            f"Stars {p0.get('stars')} | "
            f"hand={p0.get('hand_count')} draw={p0.get('draw_count')} "
            f"discard={p0.get('discard_count')} exhaust={p0.get('exhaust_count')} "
            f"play={p0.get('play_count')}"
        )

    # Enemies — use canonical ordering (combat_id asc, hittable first).
    enemies = [c for c in combat.get("creatures") or [] if not c.get("is_player")]
    enemies.sort(key=lambda c: (not c.get("is_hittable"), c.get("combat_id") or 0))
    if enemies:
        lines.append("Enemies:")
        for i, e in enumerate(enemies):
            letter = chr(ord("A") + i) if i < 26 else f"E{i}"
            lines.append(_format_enemy(letter, e))

    # Hand listing — show legality via can_play flag.
    if players:
        hand = (players[0].get("hand") or [])
        if hand:
            lines.append(f"Hand ({len(hand)}):")
            for i, card in enumerate(hand):
                lines.append(_format_card(i, card))
        else:
            lines.append("Hand: (empty)")

    # Action mask summary — short list of what the agent can do right now.
    if mask_payload and mask_payload.get("play_phase"):
        actions = mask_payload.get("actions") or []
        playable_cards = [a for a in actions if a.get("type") == "play_card"]
        if playable_cards:
            lines.append(f"Legal actions: {len(playable_cards)} card(s) playable + end_turn")
        else:
            lines.append("Legal actions: end_turn only")

    return "\n".join(lines)


def render_selector(obs_payload: dict[str, Any], mask_payload: dict[str, Any] | None = None) -> str:
    """Render a pending card-selector context (dev plan §2.3 in-combat selectors + post-combat reward/upgrade/transform)."""
    sel = obs_payload.get("selector") or {}
    if not sel.get("active"):
        return "Selector inactive"

    lines: list[str] = []
    lines.append(
        f"Selector — pick {sel.get('min_select')}..{sel.get('max_select')} of "
        f"{len(sel.get('options') or [])} option(s)"
    )
    accumulator = sel.get("accumulator") or []
    if accumulator:
        lines.append(f"  Already picked: {accumulator}")
    for opt in sel.get("options") or []:
        idx = opt.get("option_idx")
        name = strip_bbcode(opt.get("card_id") or "?")
        cost = opt.get("cost")
        cost_str = f"cost {cost}" if cost is not None else "cost ?"
        flags = []
        if opt.get("is_upgraded"):
            flags.append("+")
        tgt = opt.get("target_type")
        if tgt and tgt not in ("None", "Self"):
            flags.append(f"target={tgt}")
        marker = "★" if idx in accumulator else " "
        suffix = f" [{' '.join(flags)}]" if flags else ""
        lines.append(f"  {marker} [{idx}] {name} ({cost_str}){suffix}")

    if mask_payload and mask_payload.get("selector_active"):
        n_pick = sum(1 for a in mask_payload.get("actions", []) if a.get("type") == "select_pick")
        bits = [f"pick × {n_pick}"]
        if any(a.get("type") == "select_unpick" for a in mask_payload.get("actions", [])):
            bits.append("unpick available")
        if any(a.get("type") == "select_confirm" for a in mask_payload.get("actions", [])):
            bits.append("can confirm")
        if any(a.get("type") == "select_skip" for a in mask_payload.get("actions", [])):
            bits.append("can skip")
        lines.append("Legal: " + " | ".join(bits))

    return "\n".join(lines)


def render_text(obs_payload: dict[str, Any], mask_payload: dict[str, Any] | None = None) -> str:
    """Top-level renderer — dispatches on ``phase`` and selector activation.

    Day-8.1: selector phase takes precedence over combat rendering. If both a
    selector and a combat state are active simultaneously (in-combat selector
    interrupts), we render both — selector first, then the combat backdrop.
    """
    phase = obs_payload.get("phase")
    in_run = obs_payload.get("in_run")
    selector_active = (obs_payload.get("selector") or {}).get("active")

    header_bits = [f"phase={phase}"]
    if in_run is not None:
        header_bits.append(f"in_run={in_run}")
    if selector_active:
        header_bits.append("selector=on")
    if obs_payload.get("partial"):
        header_bits.append("partial=true")
    header = " | ".join(header_bits)

    sections: list[str] = []
    if selector_active:
        sections.append(render_selector(obs_payload, mask_payload))
    if obs_payload.get("combat"):
        sections.append(render_combat(obs_payload, mask_payload))
    elif not selector_active:
        sections.append(f"(no detailed renderer for phase={phase!r} yet — Day-8 covers combat + selector)")

    return f"=== STS2 obs ({header}) ===\n" + "\n---\n".join(sections)


def render_json(obs_payload: dict[str, Any], mask_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Day-11.A: structured JSON view for LLM tool-use mode.

    Same underlying state as :func:`render_text`, but returns a dict tree
    instead of formatted prose. Useful when prompting LLMs that respond best
    in tool-call / JSON-output mode (Claude tool_use, GPT functions).

    Schema is intentionally lean — only fields a human-equivalent observer can
    see, normalized for LLM consumption (no Godot internal fields, no wall-
    clock-y fields like ``snapshot_age_ms``).
    """
    phase = obs_payload.get("phase")
    out: dict[str, Any] = {
        "phase": phase,
        "in_run": obs_payload.get("in_run"),
        "partial": obs_payload.get("partial", False),
    }
    sel = obs_payload.get("selector") or {}
    if sel.get("active"):
        out["selector"] = {
            "min_select": sel.get("min_select"),
            "max_select": sel.get("max_select"),
            "accumulator": list(sel.get("accumulator") or []),
            "options": [
                {
                    "option_idx": o.get("option_idx"),
                    "card_id": strip_bbcode(o.get("card_id")),
                    "cost": o.get("cost"),
                    "is_upgraded": o.get("is_upgraded"),
                    "target_type": o.get("target_type"),
                }
                for o in (sel.get("options") or [])
            ],
        }

    combat = obs_payload.get("combat") or {}
    if combat:
        creatures = combat.get("creatures") or []
        player_c = next((c for c in creatures if c.get("is_player")), None)
        # Hide dead/non-hittable creatures — corpses aren't actionable and just
        # confuse the prompt.
        enemies = sorted(
            [c for c in creatures if not c.get("is_player") and c.get("is_alive")],
            key=lambda c: c.get("combat_id") or 0,
        )
        p0 = (combat.get("players") or [{}])[0]
        out["combat"] = {
            "encounter": combat.get("encounter"),
            "round": combat.get("round"),
            "play_phase": combat.get("play_phase"),
            "player": {
                "hp": (player_c or {}).get("current_hp"),
                "max_hp": (player_c or {}).get("max_hp"),
                "block": (player_c or {}).get("block") or 0,
                "energy": p0.get("energy"),
                "max_energy": p0.get("max_energy"),
                "stars": p0.get("stars"),
                "powers": [
                    {"id": p.get("id"), "amount": p.get("amount")}
                    for p in ((player_c or {}).get("powers") or [])
                ],
            },
            "enemies": [
                {
                    "letter": chr(ord("A") + i) if i < 26 else f"E{i}",
                    "combat_id": e.get("combat_id"),
                    "monster_id": strip_bbcode(e.get("monster_id")),
                    "hp": e.get("current_hp"),
                    "max_hp": e.get("max_hp"),
                    "block": e.get("block") or 0,
                    "powers": [
                        {"id": p.get("id"), "amount": p.get("amount")}
                        for p in (e.get("powers") or [])
                    ],
                    "intent": _summarize_intent(e.get("next_move")),
                }
                for i, e in enumerate(enemies)
            ],
            "piles": {
                "hand": [_card_view(c) for c in (p0.get("hand") or [])],
                "draw_count": p0.get("draw_count"),
                "discard_count": p0.get("discard_count"),
                "exhaust_count": p0.get("exhaust_count"),
                "play_count": p0.get("play_count"),
            },
        }
    if "map" in obs_payload:
        out["map"] = obs_payload["map"]
    if "event" in obs_payload:
        out["event"] = obs_payload["event"]
    if "reward" in obs_payload:
        out["reward"] = obs_payload["reward"]
    if "game_over" in obs_payload:
        out["game_over"] = obs_payload["game_over"]
    return out


def _card_view(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": strip_bbcode(c.get("id")),
        "cost": c.get("cost"),
        "is_upgraded": c.get("is_upgraded"),
        "target_type": c.get("target_type"),
        "can_play": c.get("can_play"),
    }


def _summarize_intent(next_move: dict[str, Any] | None) -> dict[str, Any] | None:
    if not next_move: return None
    intents = next_move.get("intents") or []
    summarized = []
    for intent in intents:
        d: dict[str, Any] = {"type": intent.get("type")}
        if intent.get("total_damage") is not None and intent.get("total_damage") >= 0:
            d["damage"] = intent["total_damage"]
            d["repeats"] = intent.get("repeats", 1)
        summarized.append(d)
    return {"id": next_move.get("id"), "intents": summarized}


__all__ = ["render_text", "render_combat", "render_selector", "render_json", "strip_bbcode"]
