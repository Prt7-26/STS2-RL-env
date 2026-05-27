"""Day-11.B: robust LLM-output → structured action parser.

LLMs produce action text in three rough flavors:

  1. **Pure canonical** — exactly the format ``action_codec.from_text`` expects:

         play Strike on B

  2. **Prose + canonical somewhere inside**:

         I should burn through the front-line cultist first since it's
         buffing the other two. play Strike on A

  3. **Tool-use / JSON**:

         {"action": "play_card", "card": "Strike", "target": "B"}

This parser tries each strategy in order, returning the first successful
structured action. On total failure it raises :class:`ParseError` (re-raised
from action_codec) — wrappers can decide whether to retry the model or take
a default action.

Synonym normalization handles minor variations LLMs love:
  * "attack with X" / "cast X" / "use X" → "play X"
  * "click X" / "select X" → "select pick X"
  * "go to map X,Y" / "move to X,Y" → "choose map X,Y"

All matching is case-insensitive; whitespace and trailing punctuation get
collapsed before pattern matching.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sts2_gym.action_codec import ParseError, from_text

# Synonym rewrite: applied as a pre-pass before canonical-text matching.
_SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:attack|cast|use|invoke)\s+with\s+", re.IGNORECASE), "play "),
    (re.compile(r"\b(?:cast|use)\s+", re.IGNORECASE), "play "),
    (re.compile(r"\b(?:click|select)\s+option\s+", re.IGNORECASE), "choose option "),
    (re.compile(r"\bgo\s+to\s+map\s+", re.IGNORECASE), "choose map "),
    (re.compile(r"\bmove\s+to\s+(?=\d)", re.IGNORECASE), "choose map "),
    (re.compile(r"\b(?:end|finish|pass)\s+(?:my\s+)?turn\b", re.IGNORECASE), "end turn"),
    (re.compile(r"\b(?:exit|close|skip)\s+(?:the\s+)?reward[s]?\b", re.IGNORECASE), "leave reward"),
    (re.compile(r"\b(?:buy|purchase)\s+item\s+(\d+)\b", re.IGNORECASE), r"shop buy \1"),
    (re.compile(r"\b(?:leave|exit)\s+(?:the\s+)?shop\b", re.IGNORECASE), "shop leave"),
]

# Lines that look like they contain an action. Try each candidate.
_CANDIDATE_PATTERNS = (
    r"\bplay\b",
    r"\bend\s+turn\b",
    r"\bselect\s+(?:pick|unpick|confirm|skip)\b",
    r"\bchoose\s+(?:map|option)\b",
    r"\bleave\s+reward\b",
    r"\bproceed\b",
    r"\bshop\s+(?:buy|leave)\b",
    r"\brest\s+\d+",
)
_CANDIDATE_RE = re.compile("|".join(_CANDIDATE_PATTERNS), re.IGNORECASE)

# Tool-use JSON: {"action": "play_card", ...} or {"type": "play_card", ...}
_JSON_RE = re.compile(r"\{[^{}]*?(?:\"action\"|\"type\")[^{}]*?\}", re.DOTALL)


class LLMActionParser:
    """Robust parser that extracts a structured action from LLM output.

    Parameters
    ----------
    context :
        Optional /observe payload, passed through to ``action_codec.from_text``
        for card-name / target-letter resolution.
    on_ambiguity :
        Behavior when multiple candidate actions parse successfully:
        ``"first"`` (default) returns the first one in the input; ``"last"``
        returns the last (closer to the LLM's final answer).
    """

    def __init__(self, context: dict[str, Any] | None = None, on_ambiguity: str = "last"):
        if on_ambiguity not in ("first", "last"):
            raise ValueError(f"on_ambiguity must be 'first' or 'last', got {on_ambiguity!r}")
        self.context = context
        self.on_ambiguity = on_ambiguity

    def parse(self, text: str) -> dict[str, Any]:
        if not text or not text.strip():
            raise ParseError("empty input")

        # Strategy 1: tool-use JSON.
        action = self._try_json(text)
        if action is not None:
            return action

        # Strategy 2: synonym-normalized candidate scan.
        normalized = self._normalize(text)
        candidates = self._extract_candidates(normalized)
        if not candidates:
            # Last resort — try the raw last line.
            for line in reversed(text.splitlines()):
                s = line.strip()
                if s:
                    try:
                        return from_text(self._normalize(s), context=self.context)
                    except ParseError:
                        continue
            raise ParseError(f"no recognizable action found in: {text[:200]!r}")

        ordered = candidates if self.on_ambiguity == "first" else list(reversed(candidates))
        last_err: ParseError | None = None
        for cand in ordered:
            # The canonical parser anchors to ^/$, so trailing words ("play
            # strike on A instead") would fail unless we trim. Try the full
            # candidate first, then progressively drop trailing tokens until
            # we either parse successfully or run out of words.
            for trimmed in self._trim_candidates(cand):
                try:
                    return from_text(trimmed, context=self.context)
                except ParseError as e:
                    last_err = e
                    continue
        raise last_err or ParseError(f"no candidate parsed: {candidates!r}")

    @staticmethod
    def _trim_candidates(text: str) -> list[str]:
        """Yield candidate strings: the full text, then progressively shorter
        right-trimmed versions. Lets the strict parser succeed on inputs like
        ``"play strike on A instead"`` by trying ``"play strike on A"`` next.
        """
        # Also split at sentence punctuation — "play X. Then..." → ["play X. Then...", "play X"]
        # Tokens are word-ish runs separated by spaces/punctuation.
        out: list[str] = [text]
        # Cut at commas / semicolons / periods.
        for sep in (".", ",", ";"):
            if sep in text:
                head = text.split(sep)[0].strip()
                if head and head not in out:
                    out.append(head)
        # Progressively drop trailing tokens.
        tokens = text.split()
        while len(tokens) > 1:
            tokens.pop()
            candidate = " ".join(tokens)
            if candidate not in out:
                out.append(candidate)
        return out

    # ---- internals ----

    @staticmethod
    def _normalize(text: str) -> str:
        s = text
        for pat, repl in _SYNONYMS:
            s = pat.sub(repl, s)
        # Strip surrounding punctuation that throws off our regex anchors.
        s = re.sub(r"[\.!?;]+\s*$", "", s.strip())
        return s

    @staticmethod
    def _extract_candidates(text: str) -> list[str]:
        """Find spans of text that look like canonical actions."""
        # First try the whole input as-is — most efficient when LLM was strict.
        candidates: list[str] = []
        whole = text.strip()
        if _CANDIDATE_RE.search(whole):
            candidates.append(whole)
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if _CANDIDATE_RE.search(s):
                if s not in candidates:
                    candidates.append(s)
                # Also try keyword-anchored substring of the line.
                for m in _CANDIDATE_RE.finditer(s):
                    sub = s[m.start():].strip()
                    if sub not in candidates:
                        candidates.append(sub)
        return candidates

    def _try_json(self, text: str) -> dict[str, Any] | None:
        for m in _JSON_RE.finditer(text):
            blob = m.group(0)
            try:
                data = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            action = self._json_to_structured(data)
            if action is not None:
                return action
        return None

    def _json_to_structured(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Translate a tool-use-ish JSON object into our structured wire format.

        Accepts the canonical {"type": ..., ...} shape (passthrough) and a more
        relaxed {"action": "play_card", "card": "Strike", "target": "B"} shape.
        Returns None on shapes we can't recognize.
        """
        t = data.get("type") or data.get("action")
        if not t: return None
        if t == "play_card":
            out: dict[str, Any] = {"type": "play_card"}
            if "card_idx" in data:
                out["card_idx"] = int(data["card_idx"])
            elif "card" in data and self.context:
                resolved = from_text(f"play {data['card']}", context=self.context)
                if "card_idx" in resolved: out["card_idx"] = resolved["card_idx"]
            else:
                return None
            tgt = data.get("target_combat_id") or data.get("target")
            if tgt is not None:
                if isinstance(tgt, int):
                    out["target_combat_id"] = tgt
                elif self.context:
                    resolved = from_text(f"play {data.get('card', 'X')} on {tgt}", context=self.context)
                    if "target_combat_id" in resolved: out["target_combat_id"] = resolved["target_combat_id"]
            return out
        # Direct passthrough for trivial types.
        if t in ("end_turn", "select_confirm", "select_skip", "leave_reward_screen",
                 "proceed_after_game_over", "shop_leave"):
            return {"type": t}
        if t == "select_pick" or t == "select_unpick":
            if "option_idx" not in data: return None
            return {"type": t, "option_idx": int(data["option_idx"])}
        if t == "choose_map_node":
            if "col" not in data or "row" not in data: return None
            return {"type": t, "col": int(data["col"]), "row": int(data["row"])}
        if t == "choose_event_option":
            if "option_idx" not in data: return None
            return {"type": t, "option_idx": int(data["option_idx"])}
        if t == "shop_buy":
            if "entry_idx" not in data: return None
            return {"type": t, "entry_idx": int(data["entry_idx"])}
        if t == "rest_choose":
            if "option_idx" not in data: return None
            return {"type": t, "option_idx": int(data["option_idx"])}
        return None


__all__ = ["LLMActionParser"]
