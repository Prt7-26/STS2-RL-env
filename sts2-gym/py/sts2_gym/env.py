"""Gymnasium.Env wrapper — Day-7 P0 MVP.

Scope (Day-7):
  * Combat phase only — non-combat phases (map/event/shop/...) need the
    ICardSelector hooks in the mod (dev plan §2.3, P0 but not yet built).
  * Level-A reset only — requires pre-existing run. The encounter to jump
    to is fixed at env construction; reset() restores the player+rng
    snapshot captured on first reset and re-enters that encounter.
  * Discrete action space + ``info["action_mask"]`` boolean array,
    compatible with sb3-contrib MaskablePPO (canonical RL contract).

Action encoding (flat Discrete):

    For card_idx in [0..HAND_MAX-1]:
        For enemy_slot in [0..ENEMY_MAX]:
            idx = card_idx * (ENEMY_MAX + 1) + enemy_slot
                  # enemy_slot 0 = no target / Self / AllEnemies / RandomEnemy
                  # enemy_slot k>0 = enemy at index k-1 in the canonical
                  #   hittable-enemy list (sorted by combat_id ascending)
    end_turn_idx = HAND_MAX * (ENEMY_MAX + 1)
    Total = HAND_MAX * (ENEMY_MAX + 1) + 1

Defaults: HAND_MAX=10, ENEMY_MAX=6 → action space = 71. The mask is
re-derived each step from the mod's /action_mask response so illegal
indices never make it to /step.
"""
from __future__ import annotations

from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from sts2_gym.client import ModBridgeClient, StepError

HAND_MAX = 10
ENEMY_MAX = 6
END_TURN_IDX = HAND_MAX * (ENEMY_MAX + 1)
ACTION_DIM = END_TURN_IDX + 1

# TargetType strings emitted by the mod's CombatSnapshot — must stay in sync
# with the C# enum MegaCrit.Sts2.Core.Models.Cards.TargetType.
TARGET_TYPES = (
    "None",
    "Self",
    "AnyEnemy",
    "AllEnemies",
    "RandomEnemy",
    "AnyPlayer",
    "AnyAlly",
    "AllAllies",
    "TargetedNoCreature",
    "Osty",
)
TARGET_TYPE_TO_IDX = {t: i for i, t in enumerate(TARGET_TYPES)}


def _canonical_enemies(combat: dict[str, Any]) -> list[dict[str, Any]]:
    """Hittable enemies sorted by combat_id — index in this list == enemy_slot - 1."""
    creatures = combat.get("creatures") or []
    enemies = [c for c in creatures if not c.get("is_player") and c.get("is_hittable")]
    enemies.sort(key=lambda c: (c.get("combat_id") or 0))
    return enemies


def decode_action(action_idx: int, mask_payload: dict[str, Any], combat: dict[str, Any]) -> dict[str, Any]:
    """Translate flat Discrete index → structured action dict for /step."""
    if action_idx == END_TURN_IDX:
        return {"type": "end_turn"}
    card_idx, enemy_slot = divmod(int(action_idx), ENEMY_MAX + 1)
    out: dict[str, Any] = {"type": "play_card", "card_idx": card_idx}
    if enemy_slot > 0:
        enemies = _canonical_enemies(combat)
        if enemy_slot - 1 >= len(enemies):
            raise ValueError(
                f"action {action_idx}: enemy_slot={enemy_slot} but only "
                f"{len(enemies)} hittable enemies"
            )
        out["target_combat_id"] = enemies[enemy_slot - 1]["combat_id"]
    return out


def build_action_mask(mask_payload: dict[str, Any], combat: dict[str, Any]) -> np.ndarray:
    """Convert the mod's /action_mask response into a fixed-length bool array."""
    mask = np.zeros(ACTION_DIM, dtype=bool)
    if not mask_payload.get("play_phase"):
        return mask

    # Canonical enemy ordering — must match decode_action.
    enemies = _canonical_enemies(combat)
    combat_id_to_slot = {e["combat_id"]: i + 1 for i, e in enumerate(enemies)}

    for action in mask_payload.get("actions", []):
        t = action.get("type")
        if t == "end_turn":
            mask[END_TURN_IDX] = True
            continue
        if t != "play_card":
            continue
        card_idx = action.get("card_idx")
        if card_idx is None or card_idx >= HAND_MAX:
            continue
        legal_targets = action.get("legal_targets") or []
        if action.get("requires_target"):
            for tgt in legal_targets:
                slot = combat_id_to_slot.get(tgt.get("combat_id"))
                if slot is not None and slot <= ENEMY_MAX:
                    mask[card_idx * (ENEMY_MAX + 1) + slot] = True
        else:
            mask[card_idx * (ENEMY_MAX + 1) + 0] = True
    return mask


def encode_observation(obs_payload: dict[str, Any]) -> dict[str, np.ndarray]:
    """Pure function: bridge /observe payload → tensor dict matching observation_space."""
    in_combat = 1 if obs_payload.get("phase") == "combat" and obs_payload.get("combat") else 0
    combat = obs_payload.get("combat") or {}
    players = combat.get("players") or [{}]
    p0 = players[0] if players else {}

    player_hp_creature = next(
        (c for c in (combat.get("creatures") or []) if c.get("is_player") and c.get("is_alive")),
        {},
    )

    player_vec = np.array(
        [
            int(player_hp_creature.get("current_hp") or 0),
            int(player_hp_creature.get("max_hp") or 0),
            int(player_hp_creature.get("block") or 0),
            int(p0.get("energy") or 0),
            int(p0.get("max_energy") or 0),
            int(p0.get("stars") or 0),
        ],
        dtype=np.int32,
    )

    enemies = _canonical_enemies(combat)
    enemies_arr = np.full((ENEMY_MAX, 6), -1, dtype=np.int32)
    for i, e in enumerate(enemies[:ENEMY_MAX]):
        intent_dmg = -1
        nm = e.get("next_move") or {}
        for intent in nm.get("intents") or []:
            if intent.get("type") == "Attack":
                intent_dmg = max(intent_dmg, int(intent.get("total_damage", -1) or -1))
                break
        enemies_arr[i] = [
            1 if e.get("is_alive") else 0,
            1 if e.get("is_hittable") else 0,
            int(e.get("current_hp") or 0),
            int(e.get("max_hp") or 0),
            int(e.get("block") or 0),
            intent_dmg,
        ]

    hand_arr = np.full((HAND_MAX, 4), -1, dtype=np.int32)
    for i, card in enumerate((p0.get("hand") or [])[:HAND_MAX]):
        hand_arr[i] = [
            1,  # present
            int(card.get("cost") if card.get("cost") is not None else -1),
            1 if card.get("can_play") else 0,
            TARGET_TYPE_TO_IDX.get(card.get("target_type", ""), 0),
        ]

    counts = np.array(
        [
            int(p0.get("hand_count") or 0),
            int(p0.get("draw_count") or 0),
            int(p0.get("discard_count") or 0),
            int(p0.get("exhaust_count") or 0),
            int(p0.get("play_count") or 0),
        ],
        dtype=np.int32,
    )

    return {
        "in_combat": np.int64(in_combat),
        "round": np.int32(combat.get("round") or 0),
        "player": player_vec,
        "enemies": enemies_arr,
        "hand": hand_arr,
        "counts": counts,
    }


class STS2CombatEnv(gym.Env):
    """Single-combat Gymnasium env over the in-game HTTP bridge.

    Parameters
    ----------
    encounter :
        Encounter id to jump to on reset. If None, uses the currently-active
        encounter at construction time.
    client :
        Pre-built ModBridgeClient. Defaults to the env-var-resolved port.
    max_steps :
        Truncation horizon (in /step calls, not in-game rounds).
    reward_mode :
        ``"sparse"`` (default): +1 win / -1 loss at combat end, 0 otherwise.
        ``"shaped"``: also adds (hp_delta / max_hp) per step.
    render_mode :
        ``"ansi"`` or ``"human"`` to render a text view via HumanRenderer.

    Notes
    -----
    * **Determinism**: snapshots player + RunRngSet on first reset(); every
      subsequent reset restores from those snapshots. To re-snapshot (e.g.
      after the user manually progressed the run), pass
      ``options={"resnapshot": True}`` to reset.
    * **Singleton**: STS2 is a process singleton (dev plan §2.7). Don't
      instantiate two envs against the same game process.
    """

    metadata = {"render_modes": ["ansi", "human"]}

    def __init__(
        self,
        encounter: str | None = None,
        client: ModBridgeClient | None = None,
        max_steps: int = 200,
        reward_mode: str = "sparse",
        render_mode: str | None = None,
    ):
        super().__init__()
        if reward_mode not in ("sparse", "shaped"):
            raise ValueError(f"reward_mode must be 'sparse' or 'shaped', got {reward_mode!r}")
        if render_mode is not None and render_mode not in self.metadata["render_modes"]:
            raise ValueError(f"render_mode must be one of {self.metadata['render_modes']}")

        self.client = client or ModBridgeClient()
        self.encounter = encounter
        self.max_steps = max_steps
        self.reward_mode = reward_mode
        self.render_mode = render_mode

        self.action_space = spaces.Discrete(ACTION_DIM)
        self.observation_space = spaces.Dict(
            {
                "in_combat": spaces.Discrete(2),
                "round": spaces.Box(low=0, high=999, shape=(), dtype=np.int32),
                "player": spaces.Box(low=-1, high=9999, shape=(6,), dtype=np.int32),
                "enemies": spaces.Box(low=-1, high=9999, shape=(ENEMY_MAX, 6), dtype=np.int32),
                "hand": spaces.Box(low=-1, high=99, shape=(HAND_MAX, 4), dtype=np.int32),
                "counts": spaces.Box(low=0, high=999, shape=(5,), dtype=np.int32),
            }
        )

        # Captured on first reset (or via options={"resnapshot": True}).
        self._player_snapshot: dict[str, Any] | None = None
        self._rng_snapshot: dict[str, Any] | None = None

        # Latest cached payloads — updated every step/reset, fed to renderer.
        self._last_obs_payload: dict[str, Any] = {}
        self._last_mask_payload: dict[str, Any] = {}
        self._steps_in_episode = 0
        self._last_player_hp: int = 0

    # ---------------------------------------------------------------- helpers

    def _snapshot_initial_state(self) -> None:
        """Capture player + RNG snapshots from current run state."""
        obs = self.client.observe()
        if not obs.get("in_run"):
            raise RuntimeError(
                "STS2CombatEnv: game is not in a run — start a run manually first "
                "(Day-7 doesn't drive main-menu UI). "
                f"phase={obs.get('phase')!r}"
            )
        self._player_snapshot = self.client.snapshot_player()
        self._rng_snapshot = self.client.snapshot_run_rng()
        # If encounter wasn't fixed at construction, pin it now.
        if self.encounter is None:
            self.encounter = (obs.get("combat") or {}).get("encounter")
            if self.encounter is None:
                raise RuntimeError(
                    "STS2CombatEnv: no encounter given and not currently in combat — "
                    "pass encounter='...' to the constructor."
                )

    def _refresh_caches(self) -> None:
        """Pull latest /observe and /action_mask into the env's caches."""
        self._last_obs_payload = self.client.observe()
        self._last_mask_payload = self.client.action_mask()

    def _player_hp_now(self) -> int:
        combat = self._last_obs_payload.get("combat") or {}
        for c in combat.get("creatures") or []:
            if c.get("is_player") and c.get("is_alive"):
                return int(c.get("current_hp") or 0)
        return 0

    def _build_step_return(
        self,
        terminated: bool,
        truncated: bool,
        reward: float,
        extra_info: dict[str, Any] | None = None,
    ) -> tuple[dict[str, np.ndarray], float, bool, bool, dict[str, Any]]:
        obs = encode_observation(self._last_obs_payload)
        action_mask = build_action_mask(self._last_mask_payload, self._last_obs_payload.get("combat") or {})

        info: dict[str, Any] = {
            "action_mask": action_mask,
            "phase": self._last_obs_payload.get("phase"),
            "raw_obs": self._last_obs_payload,
            "raw_mask": self._last_mask_payload,
            "steps_in_episode": self._steps_in_episode,
        }
        try:
            from sts2_gym.renderer import render_text  # local import — optional dep
            info["text_obs"] = render_text(self._last_obs_payload, self._last_mask_payload)
        except Exception:  # noqa: BLE001
            # Renderer is best-effort; never let a render bug break the env loop.
            info["text_obs"] = None
        if extra_info:
            info.update(extra_info)
        return obs, reward, terminated, truncated, info

    # ---------------------------------------------------------------- gym api

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        super().reset(seed=seed)
        options = options or {}

        if self._player_snapshot is None or options.get("resnapshot"):
            self._snapshot_initial_state()

        assert self.encounter is not None  # set in _snapshot_initial_state
        resp = self.client.reset(
            encounter=self.encounter,
            rng_counters=self._rng_snapshot,
            player_snapshot=self._player_snapshot,
        )
        if not resp.get("ok"):
            raise RuntimeError(f"STS2CombatEnv: /reset returned ok=false: {resp}")

        # Wait for combat to actually become play_phase. The bridge caches
        # /observe + /action_mask on game events, so we just spin /observe.
        import time
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            self._refresh_caches()
            if self._last_obs_payload.get("phase") == "combat" and (
                self._last_obs_payload.get("combat") or {}
            ).get("play_phase"):
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("STS2CombatEnv: combat play_phase did not become True after reset")

        self._steps_in_episode = 0
        self._last_player_hp = self._player_hp_now()
        obs, _, _, _, info = self._build_step_return(
            terminated=False, truncated=False, reward=0.0, extra_info={"reset_resp": resp}
        )
        return obs, info

    def step(
        self, action: int
    ) -> tuple[dict[str, np.ndarray], float, bool, bool, dict[str, Any]]:
        action_idx = int(action)
        combat = self._last_obs_payload.get("combat") or {}
        action_dict = decode_action(action_idx, self._last_mask_payload, combat)

        try:
            resp = self.client.step(action_dict)
        except StepError as e:
            # Server-side rejection (illegal action, unplayable, etc.). Two things
            # must hold here to avoid the infinite-loop bug we hit in env_smoke:
            #   1. _steps_in_episode MUST increment, otherwise max_steps truncation
            #      never fires and a stale-mask + 409-storm spins forever.
            #   2. We must re-check phase after refresh: if combat ended (e.g. player
            #      died waiting for an enemy turn that we never end_turn'd through),
            #      we surface terminated=True so the episode actually ends.
            self._steps_in_episode += 1
            try:
                self._refresh_caches()
            except Exception:  # noqa: BLE001
                pass
            phase = self._last_obs_payload.get("phase")
            terminated = phase != "combat"
            truncated = self._steps_in_episode >= self.max_steps and not terminated
            return self._build_step_return(
                terminated=terminated,
                truncated=truncated,
                reward=0.0,
                extra_info={"step_error": {"status": e.status, "payload": e.payload}},
            )

        self._steps_in_episode += 1
        still_in_combat = bool(resp.get("still_in_combat", True))
        # Tolerate bridge hiccups during combat-end animation. If /observe or
        # /action_mask blows up immediately after a still_in_combat=false step,
        # we still know the episode terminated — return the pre-step caches and
        # surface the error in info.
        refresh_error: dict[str, Any] | None = None
        try:
            self._refresh_caches()
        except Exception as e:  # noqa: BLE001
            refresh_error = {"refresh_error": repr(e)}
            if still_in_combat:
                # Not a death-screen race — propagate.
                raise

        terminated = not still_in_combat or self._last_obs_payload.get("phase") != "combat"
        truncated = self._steps_in_episode >= self.max_steps and not terminated

        # ----- reward -----
        new_hp = self._player_hp_now()
        hp_delta = new_hp - self._last_player_hp
        self._last_player_hp = new_hp

        reward = 0.0
        if terminated:
            reward = 1.0 if new_hp > 0 else -1.0
        if self.reward_mode == "shaped":
            max_hp = next(
                (
                    int(c.get("max_hp") or 1)
                    for c in (self._last_obs_payload.get("combat") or {}).get("creatures") or []
                    if c.get("is_player")
                ),
                1,
            )
            reward += hp_delta / max(max_hp, 1)

        extra: dict[str, Any] = {"step_resp": resp, "hp_delta": hp_delta}
        if refresh_error:
            extra.update(refresh_error)
        return self._build_step_return(
            terminated=terminated,
            truncated=truncated,
            reward=reward,
            extra_info=extra,
        )

    def render(self) -> str | None:
        if self.render_mode is None:
            return None
        try:
            from sts2_gym.renderer import render_text
        except ImportError:
            return None
        text = render_text(self._last_obs_payload, self._last_mask_payload)
        if self.render_mode == "human":
            print(text)
            return None
        return text  # "ansi"

    def close(self) -> None:
        # Nothing to dispose — the HTTP client is stateless and the game keeps running.
        pass


__all__ = [
    "STS2CombatEnv",
    "HAND_MAX",
    "ENEMY_MAX",
    "ACTION_DIM",
    "END_TURN_IDX",
    "TARGET_TYPES",
    "encode_observation",
    "build_action_mask",
    "decode_action",
]
