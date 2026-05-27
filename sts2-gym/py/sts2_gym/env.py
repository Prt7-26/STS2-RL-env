"""Gymnasium.Env wrapper — Day-7 P0 MVP + Day-8.1 selector support.

Scope:
  * Combat phase + in-combat selector screens (Survivor's discard, etc.).
    The same selector slots also cover post-combat reward / upgrade /
    transform / enchant — but Level-A reset jumps directly to an encounter,
    so the practical Day-8 surface is in-combat selectors. Non-combat
    phases (map/event/shop/...) still need their own action types — TODO.
  * Level-A reset only — requires pre-existing run.
  * Discrete action space + ``info["action_mask"]`` boolean array,
    compatible with sb3-contrib MaskablePPO.

Action encoding (flat Discrete):

    Combat range [0, END_TURN_IDX]:
        For card_idx in [0..HAND_MAX-1]:
            For enemy_slot in [0..ENEMY_MAX]:
                idx = card_idx * (ENEMY_MAX + 1) + enemy_slot
                      # enemy_slot 0 = no target / Self / AllEnemies / RandomEnemy
                      # enemy_slot k>0 = enemy at canonical hittable-list idx k-1
        end_turn_idx = HAND_MAX * (ENEMY_MAX + 1)

    Selector range [SELECTOR_PICK_BASE, SELECTOR_SKIP_IDX]:
        select_pick(option_idx) at SELECTOR_PICK_BASE + option_idx
            # option_idx is the index into the engine's pending selector options
            # (NOT card_idx — could be in any pile, e.g. discard, draw).
        select_unpick(option_idx) at SELECTOR_UNPICK_BASE + option_idx
            # Only legal for indices currently in the accumulator.
        select_confirm_idx
            # Only legal when accumulator.size >= min_select.
        select_skip_idx
            # Only legal when min_select == 0.

Defaults: HAND_MAX=10, ENEMY_MAX=6, SELECTOR_MAX=50.
Action space size = 71 (combat) + 50 (pick) + 50 (unpick) + 2 (confirm/skip) = 173.

Which range is legal at any moment is fully expressed by ``info["action_mask"]`` —
combat actions and selector actions are mutually exclusive (engine is either
in play_phase or blocked on a selector, never both).
"""
from __future__ import annotations

from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from sts2_gym.client import ModBridgeClient, StepError

HAND_MAX = 10
ENEMY_MAX = 6
SELECTOR_MAX = 50

# Combat range
END_TURN_IDX = HAND_MAX * (ENEMY_MAX + 1)              # 70
COMBAT_LAST_IDX = END_TURN_IDX                          # 70

# Selector range
SELECTOR_PICK_BASE = COMBAT_LAST_IDX + 1                # 71
SELECTOR_UNPICK_BASE = SELECTOR_PICK_BASE + SELECTOR_MAX  # 121
SELECTOR_CONFIRM_IDX = SELECTOR_UNPICK_BASE + SELECTOR_MAX  # 171
SELECTOR_SKIP_IDX = SELECTOR_CONFIRM_IDX + 1            # 172

ACTION_DIM = SELECTOR_SKIP_IDX + 1                      # 173

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
    a = int(action_idx)
    if a == END_TURN_IDX:
        return {"type": "end_turn"}
    if a == SELECTOR_CONFIRM_IDX:
        return {"type": "select_confirm"}
    if a == SELECTOR_SKIP_IDX:
        return {"type": "select_skip"}
    if SELECTOR_PICK_BASE <= a < SELECTOR_PICK_BASE + SELECTOR_MAX:
        return {"type": "select_pick", "option_idx": a - SELECTOR_PICK_BASE}
    if SELECTOR_UNPICK_BASE <= a < SELECTOR_UNPICK_BASE + SELECTOR_MAX:
        return {"type": "select_unpick", "option_idx": a - SELECTOR_UNPICK_BASE}
    if 0 <= a < END_TURN_IDX:
        card_idx, enemy_slot = divmod(a, ENEMY_MAX + 1)
        out: dict[str, Any] = {"type": "play_card", "card_idx": card_idx}
        if enemy_slot > 0:
            enemies = _canonical_enemies(combat)
            if enemy_slot - 1 >= len(enemies):
                raise ValueError(
                    f"action {a}: enemy_slot={enemy_slot} but only "
                    f"{len(enemies)} hittable enemies"
                )
            out["target_combat_id"] = enemies[enemy_slot - 1]["combat_id"]
        return out
    raise ValueError(f"action index {a} out of range [0, {ACTION_DIM})")


def build_action_mask(mask_payload: dict[str, Any], combat: dict[str, Any]) -> np.ndarray:
    """Convert the mod's /action_mask response into a fixed-length bool array.

    Combat and selector slots are mutually exclusive — the engine is either in
    play_phase or blocked on a selector, never both. The mod signals which mode
    we're in via ``mask_payload["selector_active"]`` (Day-8.1) — for backward
    compatibility we also recognize the action ``type`` field directly.
    """
    mask = np.zeros(ACTION_DIM, dtype=bool)
    if mask_payload.get("selector_active"):
        for action in mask_payload.get("actions", []):
            t = action.get("type")
            if t == "select_pick":
                opt = action.get("option_idx")
                if opt is not None and 0 <= opt < SELECTOR_MAX:
                    mask[SELECTOR_PICK_BASE + opt] = True
            elif t == "select_unpick":
                opt = action.get("option_idx")
                if opt is not None and 0 <= opt < SELECTOR_MAX:
                    mask[SELECTOR_UNPICK_BASE + opt] = True
            elif t == "select_confirm":
                mask[SELECTOR_CONFIRM_IDX] = True
            elif t == "select_skip":
                mask[SELECTOR_SKIP_IDX] = True
        return mask

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


def encode_observation(
    obs_payload: dict[str, Any],
    registry: "Registry | None" = None,
) -> dict[str, np.ndarray]:
    """Bridge /observe payload → tensor dict matching observation_space.

    Day-9.3: if a Registry is supplied, hand / selector_options gain a
    ``card_idx`` column and enemies gain a ``monster_idx`` column. Without a
    registry (legacy path / unit tests), those columns are filled with
    UNKNOWN_IDX=0 — agent sees structurally-correct but identity-less data.
    """
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
    # Day-9.3: enemies shape (ENEMY_MAX, 7) — last column is monster_idx.
    enemies_arr = np.full((ENEMY_MAX, 7), -1, dtype=np.int32)
    for i, e in enumerate(enemies[:ENEMY_MAX]):
        intent_dmg = -1
        nm = e.get("next_move") or {}
        for intent in nm.get("intents") or []:
            if intent.get("type") == "Attack":
                intent_dmg = max(intent_dmg, int(intent.get("total_damage", -1) or -1))
                break
        monster_idx = registry.monster_idx(e.get("monster_id")) if registry else 0
        enemies_arr[i] = [
            1 if e.get("is_alive") else 0,
            1 if e.get("is_hittable") else 0,
            int(e.get("current_hp") or 0),
            int(e.get("max_hp") or 0),
            int(e.get("block") or 0),
            intent_dmg,
            monster_idx,
        ]

    # Day-9.3: hand shape (HAND_MAX, 5) — last column is card_idx.
    hand_arr = np.full((HAND_MAX, 5), -1, dtype=np.int32)
    for i, card in enumerate((p0.get("hand") or [])[:HAND_MAX]):
        card_idx = registry.card_idx(card.get("id")) if registry else 0
        hand_arr[i] = [
            1,  # present
            int(card.get("cost") if card.get("cost") is not None else -1),
            1 if card.get("can_play") else 0,
            TARGET_TYPE_TO_IDX.get(card.get("target_type", ""), 0),
            card_idx,
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

    # Day-8.1 + Day-9.3: selector_options shape (SELECTOR_MAX, 5).
    selector = obs_payload.get("selector") or {}
    selector_active_int = 1 if selector.get("active") else 0
    selector_options = np.full((SELECTOR_MAX, 5), -1, dtype=np.int32)
    if selector.get("active"):
        for i, opt in enumerate((selector.get("options") or [])[:SELECTOR_MAX]):
            card_idx = registry.card_idx(opt.get("card_id")) if registry else 0
            selector_options[i] = [
                1,
                int(opt.get("cost") if opt.get("cost") is not None else -1),
                1 if opt.get("is_upgraded") else 0,
                TARGET_TYPE_TO_IDX.get(opt.get("target_type", ""), 0),
                card_idx,
            ]
    selector_scalars = np.array(
        [
            selector_active_int,
            int(selector.get("min_select") or 0),
            int(selector.get("max_select") or 0),
            len(selector.get("accumulator") or []),
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
        "selector": selector_scalars,
        "selector_options": selector_options,
    }


class STS2CombatEnv(gym.Env):
    """Single-combat Gymnasium env over the in-game HTTP bridge.

    Parameters
    ----------
    encounter :
        Encounter id to jump to on reset. If None, uses the currently-active
        encounter at construction time.
    character :
        If set (Day-9.2), env starts a fresh single-player run on first reset
        via /start_run before snapshotting + jumping to ``encounter``. One of
        ``"IRONCLAD"`` / ``"SILENT"`` / ``"DEFECT"`` / ``"NECROBINDER"`` /
        ``"REGENT"``. If None, env requires user to already be in a run
        (legacy Level-A behavior).
    ascension :
        0..10. Only used when ``character`` is set.
    run_seed :
        Optional seed for the fresh run. Only used when ``character`` is set.
    client :
        Pre-built ModBridgeClient. Defaults to the env-var-resolved port.
    max_steps :
        Truncation horizon (in /step calls, not in-game rounds).
    reward_mode :
        ``"sparse"`` (default): +1 win / -1 loss at combat end, 0 otherwise.
        ``"shaped"``: also adds (hp_delta / max_hp) per step.
    render_mode :
        ``"ansi"`` or ``"human"`` to render a text view via HumanRenderer.
    partial_obs :
        If True, fetches partial /observe (RNG counters + RelicGrabBag masked
        per Day-9.4). LLM-evaluation default.
    registry :
        Optional pre-loaded :class:`~sts2_gym.registry.Registry`. If None and
        ``use_registry=True``, env loads it lazily on first reset.
    use_registry :
        Default True (Day-9.3). Set False to skip card_id / monster_id
        integer encoding (debug / unit tests).

    Notes
    -----
    * **Determinism**: snapshots player + RunRngSet on first reset(); every
      subsequent reset restores from those snapshots. To re-snapshot (e.g.
      after the user manually progressed the run), pass
      ``options={"resnapshot": True}`` to reset.
    * **Singleton**: STS2 is a process singleton (dev plan §2.7). Don't
      instantiate two envs against the same game process.
    * **Selector toggle**: Day-9.1, env auto-enables ICardSelector on __init__
      and disables on close(). Manual play remains unintercepted between
      sessions.
    """

    metadata = {"render_modes": ["ansi", "human"]}

    def __init__(
        self,
        encounter: str | None = None,
        character: str | None = None,
        ascension: int = 0,
        run_seed: str | None = None,
        client: ModBridgeClient | None = None,
        max_steps: int = 200,
        reward_mode: str = "sparse",
        render_mode: str | None = None,
        partial_obs: bool = False,
        registry: "Registry | None" = None,
        use_registry: bool = True,
    ):
        super().__init__()
        if reward_mode not in ("sparse", "shaped"):
            raise ValueError(f"reward_mode must be 'sparse' or 'shaped', got {reward_mode!r}")
        if render_mode is not None and render_mode not in self.metadata["render_modes"]:
            raise ValueError(f"render_mode must be one of {self.metadata['render_modes']}")
        if ascension < 0 or ascension > 10:
            raise ValueError(f"ascension must be 0..10, got {ascension}")

        self.client = client or ModBridgeClient()
        self.encounter = encounter
        self.character = character.upper() if character else None
        self.ascension = ascension
        self.run_seed = run_seed
        self.max_steps = max_steps
        self.reward_mode = reward_mode
        self.render_mode = render_mode
        self.partial_obs = partial_obs
        self._registry: "Registry | None" = registry
        self._use_registry = use_registry

        # Day-9.1: enable selector for this env session. Disabled in close().
        # Tolerate failures (bridge might still be coming up at __init__ time).
        try:
            self.client.enable_selector()
        except Exception as e:
            import warnings
            warnings.warn(f"STS2CombatEnv: failed to enable selector — {e}")
        self._selector_enabled = True

        self.action_space = spaces.Discrete(ACTION_DIM)
        self.observation_space = spaces.Dict(
            {
                "in_combat": spaces.Discrete(2),
                "round": spaces.Box(low=0, high=999, shape=(), dtype=np.int32),
                "player": spaces.Box(low=-1, high=9999, shape=(6,), dtype=np.int32),
                # Day-9.3: enemies = 7 cols (added monster_idx)
                "enemies": spaces.Box(low=-1, high=9999, shape=(ENEMY_MAX, 7), dtype=np.int32),
                # Day-9.3: hand = 5 cols (added card_idx)
                "hand": spaces.Box(low=-1, high=10**6, shape=(HAND_MAX, 5), dtype=np.int32),
                "counts": spaces.Box(low=0, high=999, shape=(5,), dtype=np.int32),
                "selector": spaces.Box(low=0, high=10**9, shape=(4,), dtype=np.int32),
                # Day-9.3: selector_options = 5 cols
                "selector_options": spaces.Box(low=-1, high=10**6, shape=(SELECTOR_MAX, 5), dtype=np.int32),
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

    def _ensure_registry(self) -> "Registry | None":
        """Lazy-load registry on first reset. Cached for subsequent calls."""
        if not self._use_registry:
            return None
        if self._registry is None:
            try:
                from sts2_gym.registry import Registry
                self._registry = Registry.load(self.client)
            except Exception as e:
                import warnings
                warnings.warn(f"STS2CombatEnv: registry load failed ({e}); "
                              f"card/monster idx columns will be 0 (UNKNOWN)")
                self._use_registry = False
        return self._registry

    # ---------------------------------------------------------------- helpers

    def _snapshot_initial_state(self) -> None:
        """Capture player + RNG snapshots from current run state.

        Day-9.2: if ``character`` is set and we're not already in a run, start
        one fresh via /start_run before snapshotting. Otherwise fall back to
        the legacy "must already be in a run" Level-A behavior.
        """
        obs = self.client.observe()
        if not obs.get("in_run"):
            if self.character is None:
                raise RuntimeError(
                    "STS2CombatEnv: game is not in a run. Either start one "
                    "manually from the main menu, or pass character='...' to "
                    "auto-start a fresh run via /start_run. "
                    f"phase={obs.get('phase')!r}"
                )
            # Day-9.2: fresh-run via direct API.
            self.client.start_run(self.character, ascension=self.ascension, seed=self.run_seed)
            import time
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                obs = self.client.observe()
                if obs.get("in_run"):
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError("STS2CombatEnv: run did not start within 5s of /start_run")
        self._player_snapshot = self.client.snapshot_player()
        self._rng_snapshot = self.client.snapshot_run_rng()
        # If encounter wasn't fixed at construction, pin it now.
        if self.encounter is None:
            self.encounter = (obs.get("combat") or {}).get("encounter")
            if self.encounter is None:
                hint = (
                    "When auto-starting a run with character='...', you must also pass "
                    "encounter='...' — SetUpNewSinglePlayer creates the RunState but "
                    "leaves the scene at main menu; /reset's EnterRoomDebug needs an "
                    "explicit target encounter. Example: "
                    "STS2CombatEnv(character='IRONCLAD', encounter='CHOMPERS_NORMAL')."
                ) if self.character else (
                    "Start a run manually from the main menu first, OR pass "
                    "character='...' + encounter='...' to auto-start."
                )
                raise RuntimeError(f"STS2CombatEnv: no encounter set. {hint}")

    def _refresh_caches(self) -> None:
        """Pull latest /observe and /action_mask into the env's caches."""
        self._last_obs_payload = self.client.observe(partial=self.partial_obs)
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
        registry = self._registry  # registry was loaded by _ensure_registry on first reset
        obs = encode_observation(self._last_obs_payload, registry=registry)
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
            # Day-9.3: load registry on first reset (after run is confirmed
            # active and ModelDb is fully initialized server-side).
            self._ensure_registry()

        assert self.encounter is not None  # set in _snapshot_initial_state
        resp = self.client.reset(
            encounter=self.encounter,
            rng_counters=self._rng_snapshot,
            player_snapshot=self._player_snapshot,
        )
        if not resp.get("ok"):
            raise RuntimeError(f"STS2CombatEnv: /reset returned ok=false: {resp}")

        # Wait for the env to be ready to accept actions. Two valid ready states:
        #   1. phase==combat AND play_phase=True (normal start-of-combat)
        #   2. selector_active=True (Day-8.1: start-of-combat relic like Gambling
        #      Chip can fire a selector BEFORE play_phase ever becomes true —
        #      the engine is waiting on our TCS, so play_phase stays false
        #      indefinitely)
        import time
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            self._refresh_caches()
            obs = self._last_obs_payload
            combat = obs.get("combat") or {}
            selector_active = (obs.get("selector") or {}).get("active")
            if selector_active or (obs.get("phase") == "combat" and combat.get("play_phase")):
                break
            time.sleep(0.1)
        else:
            raise TimeoutError(
                "STS2CombatEnv: neither play_phase nor selector became active "
                "within 10s after reset"
            )

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

        # Day-8.1: card_select / combat both keep the episode running. Only
        # truly-out-of-combat phases (game_over, between_rooms, reward, etc.)
        # AND still_in_combat=false signal episode end. selector_active inside
        # combat shows up as phase="card_select" or sometimes phase="combat"
        # with selector_active=true — both are non-terminal.
        current_phase = self._last_obs_payload.get("phase")
        selector_active = (self._last_obs_payload.get("selector") or {}).get("active")
        in_episode = (
            still_in_combat
            and (current_phase in ("combat", "card_select") or selector_active)
        )
        terminated = not in_episode
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
        # Day-9.1: disable selector so manual play after the env session isn't
        # intercepted. Tolerate failures — the game might already be torn down.
        if getattr(self, "_selector_enabled", False):
            try:
                self.client.disable_selector()
            except Exception:  # noqa: BLE001
                pass
            self._selector_enabled = False


__all__ = [
    "STS2CombatEnv",
    "HAND_MAX",
    "ENEMY_MAX",
    "ACTION_DIM",
    "END_TURN_IDX",
    "SELECTOR_MAX",
    "SELECTOR_PICK_BASE",
    "SELECTOR_UNPICK_BASE",
    "SELECTOR_CONFIRM_IDX",
    "SELECTOR_SKIP_IDX",
    "TARGET_TYPES",
    "encode_observation",
    "build_action_mask",
    "decode_action",
]
