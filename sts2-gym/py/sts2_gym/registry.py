"""Card / monster / relic id → int registry — Day-9.3.

Why: agent's tensor observation needs stable integer identities for cards in
hand / enemies on the field. Without this the policy can't distinguish "Strike"
from "Bash" — both look like "cost=1 AnyEnemy can_play=true".

Why versioned: STS2 is in EA; content can be added or renamed between patches.
The mod's /registry endpoint includes both the game's assembly version and a
sha256 over sorted ids. We cache the registry locally and re-fetch on hash
mismatch. Unknown ids at runtime fall through to UNKNOWN_IDX=0 so old policies
keep working when the game adds a new card (the policy sees "unfamiliar card"
rather than crashing).

The mod's slot assignment:
  * 0 reserved for UNKNOWN
  * 1..N alphabetical by entry id (deterministic across runs of the same
    game version)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sts2_gym.client import ModBridgeClient

UNKNOWN_IDX = 0

DEFAULT_CACHE_DIR = Path(os.environ.get("STS2GYM_CACHE_DIR",
                                        Path.home() / ".cache" / "sts2_gym"))


class Registry:
    """Stable id → int lookup with graceful unknown-id fallback.

    Construct via :meth:`load` (uses cache, fetches if missing or stale).
    """

    def __init__(self, data: dict[str, Any]):
        self.schema_version = data.get("schema_version", 1)
        self.game_version = data.get("game_version", "unknown")
        self.content_hash = data.get("content_hash", "")
        self.cards: dict[str, int] = data.get("cards") or {}
        self.monsters: dict[str, int] = data.get("monsters") or {}
        self.relics: dict[str, int] = data.get("relics") or {}
        self.encounters: list[str] = list(data.get("encounters") or [])
        self.counts: dict[str, int] = data.get("counts") or {}
        # Reverse lookup for renderer / debug.
        self._card_idx_to_id = {v: k for k, v in self.cards.items()}
        self._monster_idx_to_id = {v: k for k, v in self.monsters.items()}

    # ------------------------------------------------------------ lookups

    def card_idx(self, card_id: str | None) -> int:
        """Look up card → integer index. Unknown → :data:`UNKNOWN_IDX`."""
        if not card_id:
            return UNKNOWN_IDX
        return self.cards.get(card_id, UNKNOWN_IDX)

    def monster_idx(self, monster_id: str | None) -> int:
        if not monster_id:
            return UNKNOWN_IDX
        return self.monsters.get(monster_id, UNKNOWN_IDX)

    def relic_idx(self, relic_id: str | None) -> int:
        if not relic_id:
            return UNKNOWN_IDX
        return self.relics.get(relic_id, UNKNOWN_IDX)

    def card_id_of(self, idx: int) -> str | None:
        return self._card_idx_to_id.get(idx)

    def monster_id_of(self, idx: int) -> str | None:
        return self._monster_idx_to_id.get(idx)

    @property
    def n_cards(self) -> int:
        # Slot 0 (UNKNOWN) reserved, so total slot count = max idx + 1.
        return max(self.cards.values(), default=0) + 1

    @property
    def n_monsters(self) -> int:
        return max(self.monsters.values(), default=0) + 1

    # ------------------------------------------------------------ loaders

    @classmethod
    def load(
        cls,
        client: ModBridgeClient | None = None,
        cache_dir: Path | None = None,
        force_refresh: bool = False,
    ) -> "Registry":
        """Load registry — checks cache first, fetches from mod on miss / hash drift.

        Cache path: ``$STS2GYM_CACHE_DIR/registry.json`` (default
        ``~/.cache/sts2_gym/registry.json``).
        """
        cache_dir = cache_dir or DEFAULT_CACHE_DIR
        cache_path = cache_dir / "registry.json"

        cached: dict[str, Any] | None = None
        if not force_refresh and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
            except (json.JSONDecodeError, OSError):
                cached = None

        # Try to fetch fresh from mod. If unreachable AND we have cache, use cache.
        client = client or ModBridgeClient()
        fresh: dict[str, Any] | None = None
        try:
            fresh = client.registry()
        except Exception as e:
            if cached is None:
                raise RuntimeError(
                    f"Registry unavailable: mod /registry failed ({e!r}) and "
                    f"no cache at {cache_path}. Is STS2 running with the mod?"
                ) from e
            print(f"[sts2_gym.registry] /registry fetch failed ({e!r}); using stale cache")
            return cls(cached)

        # If hashes match, prefer cache (avoids unnecessary writes).
        if cached and cached.get("content_hash") == fresh.get("content_hash"):
            return cls(cached)

        # Persist fresh registry to cache.
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(fresh, indent=2, sort_keys=True))
        except OSError as e:
            print(f"[sts2_gym.registry] could not write cache {cache_path}: {e}")

        return cls(fresh)


__all__ = ["Registry", "UNKNOWN_IDX"]
