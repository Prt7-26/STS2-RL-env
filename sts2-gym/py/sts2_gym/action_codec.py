"""Day-11.A: bidirectional translation between structured action dicts and
canonical LLM-friendly text.

Dev plan §3.4 specifies three representations of every action:

    Discrete int  ◄──►  Structured dict  ◄──►  Canonical text
        │                    │                       │
        ▼                    ▼                       ▼
       RL                 Internal                   LLM

The structured dict ↔ Discrete int mapping lives in :mod:`sts2_gym.env`
(:func:`decode_action`, :func:`build_action_mask`). This module wires up the
structured ↔ text leg.

Why these two:
  * Structured is the canonical wire format the mod accepts on /step.
  * Canonical text is the format an LLM agent emits and an LLM eval prompt
    requests. Strict enough to parse robustly, loose enough that a model can
    learn it from a few examples.

Two scopes covered here:
  * Combat: ``play <Card> [on <Slot>]``, ``end turn``
  * Selector (Day-8.1): ``select pick <option_idx>``, ``select unpick <n>``,
    ``select confirm``, ``select skip``
  * Non-combat (Day-10.A): ``choose map <col>,<row>``, ``choose option <n>``,
    ``leave reward``, ``proceed``

Reverse parser is intentionally tolerant — see :class:`LLMActionParser` for the
robust path. This module just does the canonical, strict form. The robust
parser is Day-11.B.
"""
from __future__ import annotations

import re
from typing import Any

from sts2_gym.renderer import strip_bbcode


# ----- Structured → Text --------------------------------------------------

def _enemy_letter(slot: int) -> str:
    """Map enemy_slot index → A/B/C/... letter (matches HumanRenderer convention)."""
    if 0 <= slot < 26:
        return chr(ord("A") + slot)
    return f"E{slot}"


def to_text(action: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    """Canonical text form. Includes context to resolve target letters / card names.

    Parameters
    ----------
    action : structured action dict (as accepted by /step).
    context : optional. If supplied, used to enrich the text — e.g. with hand
              card names instead of bare card_idx. Expected shape mirrors
              ``/observe`` payload (we read ``combat.players[0].hand[i].id`` and
              the canonical enemy ordering).
    """
    t = action.get("type", "?")
    if t == "play_card":
        card_idx = action.get("card_idx")
        name = _card_name_in_hand(card_idx, context) if context else None
        head = f"play {name or f'#{card_idx}'}"
        tgt = action.get("target_combat_id")
        if tgt is not None:
            letter = _enemy_letter_for(tgt, context) if context else None
            head += f" on {letter or f'cid{tgt}'}"
        return head
    if t == "end_turn":
        return "end turn"
    if t == "select_pick":
        return f"select pick {action.get('option_idx', '?')}"
    if t == "select_unpick":
        return f"select unpick {action.get('option_idx', '?')}"
    if t == "select_confirm":
        return "select confirm"
    if t == "select_skip":
        return "select skip"
    if t == "choose_map_node":
        return f"choose map {action.get('col', '?')},{action.get('row', '?')}"
    if t == "choose_event_option":
        return f"choose option {action.get('option_idx', '?')}"
    if t == "take_reward_item":
        return f"take reward {action.get('idx', '?')}"
    if t == "leave_reward_screen":
        return "leave reward"
    if t == "proceed_after_game_over":
        return "proceed"
    if t == "shop_buy":
        return f"shop buy {action.get('entry_idx', '?')}"
    if t == "shop_leave":
        return "shop leave"
    if t == "rest_choose":
        return f"rest {action.get('option_idx', '?')}"
    if t == "rest_leave":
        return "rest leave"
    if t == "card_reward_pick":
        return f"card reward pick {action.get('idx', '?')}"
    if t == "relic_pick":
        return f"relic pick {action.get('idx', '?')}"
    if t == "bundle_pick":
        return f"bundle pick {action.get('idx', '?')}"
    if t == "noop":
        return "noop"
    return f"<{t} {action}>"


def _card_name_in_hand(card_idx: int | None, context: dict[str, Any]) -> str | None:
    if card_idx is None: return None
    try:
        hand = (context.get("combat") or {}).get("players")[0].get("hand") or []
        return strip_bbcode(hand[int(card_idx)].get("id"))
    except (IndexError, KeyError, AttributeError, TypeError):
        return None


def _enemy_letter_for(combat_id: int, context: dict[str, Any]) -> str | None:
    try:
        creatures = (context.get("combat") or {}).get("creatures") or []
        enemies = sorted(
            [c for c in creatures if not c.get("is_player") and c.get("is_hittable")],
            key=lambda c: c.get("combat_id") or 0,
        )
        for i, e in enumerate(enemies):
            if e.get("combat_id") == combat_id:
                return _enemy_letter(i)
    except (IndexError, KeyError, AttributeError, TypeError):
        pass
    return None


# ----- Text → Structured --------------------------------------------------

class ParseError(ValueError):
    """Raised when a canonical text string can't be mapped to a structured action."""


# Compile once. Each pattern returns a structured action — checked top-to-bottom.
# Whitespace-insensitive, case-insensitive (we lower() the input first).
_PATTERNS: list[tuple[re.Pattern[str], "Any"]] = []


def _register(pattern: str, builder):
    _PATTERNS.append((re.compile(r"^\s*" + pattern + r"\s*$", re.IGNORECASE), builder))


_register(r"end\s+turn", lambda m: {"type": "end_turn"})
_register(r"play\s+(?P<name>[\w '+\-]+?)\s+on\s+(?P<tgt>[A-Za-z0-9]+)",
          lambda m: {"type": "play_card", "_name": m["name"].strip(), "_tgt": m["tgt"].strip()})
_register(r"play\s+(?P<name>[\w '+\-]+)",
          lambda m: {"type": "play_card", "_name": m["name"].strip()})
_register(r"select\s+pick\s+(?P<i>\d+)",
          lambda m: {"type": "select_pick", "option_idx": int(m["i"])})
_register(r"select\s+unpick\s+(?P<i>\d+)",
          lambda m: {"type": "select_unpick", "option_idx": int(m["i"])})
_register(r"select\s+confirm", lambda m: {"type": "select_confirm"})
_register(r"select\s+skip", lambda m: {"type": "select_skip"})
_register(r"choose\s+map\s+(?P<col>\d+)\s*,\s*(?P<row>\d+)",
          lambda m: {"type": "choose_map_node", "col": int(m["col"]), "row": int(m["row"])})
_register(r"choose\s+option\s+(?P<i>\d+)",
          lambda m: {"type": "choose_event_option", "option_idx": int(m["i"])})
_register(r"take\s+reward\s+(?P<i>\d+)",
          lambda m: {"type": "take_reward_item", "idx": int(m["i"])})
_register(r"leave\s+reward", lambda m: {"type": "leave_reward_screen"})
_register(r"proceed", lambda m: {"type": "proceed_after_game_over"})
_register(r"shop\s+buy\s+(?P<i>\d+)",
          lambda m: {"type": "shop_buy", "entry_idx": int(m["i"])})
_register(r"shop\s+leave", lambda m: {"type": "shop_leave"})
_register(r"rest\s+leave", lambda m: {"type": "rest_leave"})
_register(r"rest\s+(?P<i>\d+)",
          lambda m: {"type": "rest_choose", "option_idx": int(m["i"])})
_register(r"card\s+reward\s+pick\s+(?P<i>\d+)",
          lambda m: {"type": "card_reward_pick", "idx": int(m["i"])})
_register(r"relic\s+pick\s+(?P<i>\d+)",
          lambda m: {"type": "relic_pick", "idx": int(m["i"])})
_register(r"bundle\s+pick\s+(?P<i>\d+)",
          lambda m: {"type": "bundle_pick", "idx": int(m["i"])})
_register(r"noop", lambda m: {"type": "noop"})


def from_text(text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse canonical text → structured action dict.

    Optional ``context`` (an /observe payload) lets us resolve card names →
    card_idx and target letters → combat_id. Without it, returns a partially-
    resolved dict (with ``_name`` / ``_tgt`` placeholder keys) that callers
    must reconcile with a current observation before /step.

    Raises :class:`ParseError` if the input doesn't match any canonical form.
    """
    text = text.strip()
    if not text:
        raise ParseError("empty input")

    for pat, build in _PATTERNS:
        m = pat.match(text)
        if m:
            action = build(m)
            if action.get("type") == "play_card" and context:
                action = _resolve_play_card(action, context)
            return action
    raise ParseError(f"could not parse {text!r} into a canonical action")


def _resolve_play_card(action: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Resolve _name → card_idx and _tgt → target_combat_id using context."""
    name = action.pop("_name", None)
    tgt_letter = action.pop("_tgt", None)
    if name is None:
        return action

    hand = (context.get("combat") or {}).get("players", [{}])[0].get("hand") or []
    name_upper = name.upper().replace(" ", "_")
    candidates = [
        (i, strip_bbcode(c.get("id") or ""))
        for i, c in enumerate(hand)
    ]
    # First exact card-id match.
    match = next((i for i, cid in candidates if cid == name_upper), None)
    if match is None:
        # Then case-insensitive substring match — handles "play strike" picking STRIKE_RED.
        match = next((i for i, cid in candidates if name_upper in cid), None)
    if match is None:
        raise ParseError(f"no card matching '{name}' in current hand "
                         f"(have: {[cid for _, cid in candidates]})")
    action["card_idx"] = match

    if tgt_letter is not None:
        # Letter → enemy_slot → combat_id.
        creatures = (context.get("combat") or {}).get("creatures") or []
        enemies = sorted(
            [c for c in creatures if not c.get("is_player") and c.get("is_hittable")],
            key=lambda c: c.get("combat_id") or 0,
        )
        # 'A' → 0, 'B' → 1, …
        if len(tgt_letter) == 1 and tgt_letter.isalpha():
            slot = ord(tgt_letter.upper()) - ord("A")
        elif tgt_letter.upper().startswith("E"):
            try: slot = int(tgt_letter[1:])
            except ValueError: slot = -1
        else:
            try: slot = int(tgt_letter)
            except ValueError: slot = -1
        if slot < 0 or slot >= len(enemies):
            raise ParseError(f"target '{tgt_letter}' out of range (have {len(enemies)} hittable enemies)")
        action["target_combat_id"] = enemies[slot]["combat_id"]
    return action


__all__ = ["to_text", "from_text", "ParseError"]
