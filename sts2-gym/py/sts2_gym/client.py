"""HTTP client for the in-game STS2-Gym mod's bridge endpoints.

Day-3 minimal: stdlib only (urllib + json). Day-4+ may swap in httpx if
we need async / streaming / connection pooling. For now the bridge is
read-only so a synchronous one-shot client is fine.

Endpoints (mirroring sts2-gym/mod/HttpBridge.cs):

    GET /health    — liveness probe
    GET /version   — mod + protocol version
    GET /observe   — current state snapshot (cached, refreshed on game events)

The mod writes its bound port to ``/tmp/sts2_gym.port`` on start, so when
multiple STS2 processes run (P1 VectorEnv) we can read the right port
without env-var fishing. For Day-3 single-instance we just default to 7777.
"""
from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_PORT = int(os.environ.get("STS2GYM_PORT", "7777"))
DEFAULT_HOST = os.environ.get("STS2GYM_HOST", "127.0.0.1")
PORT_LOCKFILE = Path(os.environ.get("STS2GYM_PORT_LOCKFILE", "/tmp/sts2_gym.port"))


class StepError(Exception):
    """Raised on /step or other write-path failures (non-2xx HTTP)."""
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status = status
        self.payload = payload
        super().__init__(f"step failed: status={status} payload={payload}")


def read_port_lockfile(path: Path = PORT_LOCKFILE) -> int | None:
    """Return the port written by the mod, or None if the lockfile is absent."""
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


class ModBridgeClient:
    """Synchronous HTTP client for the mod's bridge endpoints.

    Parameters
    ----------
    port :
        TCP port the mod is listening on. Defaults to ``STS2GYM_PORT`` env var,
        else 7777. Pass ``port=None`` to auto-resolve from the lockfile.
    host :
        Host to connect to. Always 127.0.0.1 in practice.
    timeout :
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        port: int | None = DEFAULT_PORT,
        host: str = DEFAULT_HOST,
        timeout: float = 5.0,
    ):
        resolved_port = port if port is not None else read_port_lockfile() or DEFAULT_PORT
        self.host = host
        self.port = resolved_port
        self.timeout = timeout
        self.base = f"http://{host}:{resolved_port}"
        # Day-14 speed-tune: lazy-init persistent HTTPConnection. Reusing TCP
        # cuts ~10-30ms per call on macOS where urllib.urlopen reconnects
        # every call. Bonus: http.client is unaffected by HTTP_PROXY env vars
        # so a running ClashX / system proxy won't intercept localhost traffic.
        self._conn: http.client.HTTPConnection | None = None

    # ---------- HTTP primitives ----------

    def _get_conn(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        return self._conn

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str | None = None,
                 timeout: float | None = None) -> tuple[int, bytes]:
        """One HTTP round-trip on the persistent connection. Reconnects once on broken-pipe / timeout."""
        # Apply timeout override per call.
        effective_timeout = timeout if timeout is not None else self.timeout
        for attempt in (0, 1):
            conn = self._get_conn()
            conn.timeout = effective_timeout
            headers: dict[str, str] = {"Connection": "keep-alive"}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, data
            except (http.client.RemoteDisconnected, ConnectionResetError,
                    BrokenPipeError, http.client.BadStatusLine, OSError) as e:
                # Stale keep-alive socket — close + retry once.
                try: conn.close()
                except Exception: pass
                self._conn = None
                if attempt == 0:
                    continue
                raise
        # unreachable
        raise RuntimeError("retry loop exited without return")

    def _get_json(self, path: str) -> dict[str, Any]:
        status, data = self._request("GET", path)
        body = data.decode("utf-8")
        if status >= 400:
            try: err = json.loads(body)
            except (json.JSONDecodeError, ValueError): err = {"raw": body}
            raise StepError(status=status, payload=err)
        return json.loads(body)

    def _post_json(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """POST JSON, return parsed response. On non-2xx, raises StepError with parsed body."""
        body_bytes = json.dumps(payload).encode("utf-8")
        status, data = self._request("POST", path, body=body_bytes,
                                     content_type="application/json", timeout=timeout)
        body = data.decode("utf-8", errors="replace")
        if status >= 400:
            try: err = json.loads(body)
            except (json.JSONDecodeError, ValueError): err = {"raw": body}
            raise StepError(status=status, payload=err)
        return json.loads(body)

    def close(self) -> None:
        """Close the persistent HTTP connection. Idempotent."""
        if self._conn is not None:
            try: self._conn.close()
            except Exception: pass
            self._conn = None

    def __enter__(self) -> "ModBridgeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- endpoint wrappers ----------

    def health(self) -> dict[str, Any]:
        """Return the mod's liveness payload (mod id, version, protocol_version, port)."""
        return self._get_json("/health")

    def version(self) -> dict[str, Any]:
        return self._get_json("/version")

    def observe(self, partial: bool = False, with_mask: bool = False) -> dict[str, Any]:
        """Return the current state snapshot.

        Parameters
        ----------
        partial :
            If True, request the PartialObs view (dev plan §2.8): hides
            information not visible to a human player. Day-4 implementation
            masks ``combat.players[*].draw_pile`` content (count preserved).
            Day-5+ will also mask RNG counters and future-reward pool.

        Always-present top-level keys:
            ``phase``           — short phase name. Day-4 enum:
                main_menu, game_over, reward, upgrade, transform, enchant,
                card_select, relic_select, combat, combat_pending, event,
                shop, rest, treasure, map, between_rooms
            ``in_run``          — bool
            ``snapshot_age_ms`` — staleness of the cached snapshot at response time
            ``partial``         — bool, echoes the mode this payload was built in

        When ``in_run`` is True:
            ``run``    — full SerializableRun JSON (dev plan §2.1 path a)
            ``combat`` — full mid-combat extension (dev plan §2.1 path b)
                         when ``phase`` is combat-y, otherwise absent
        """
        # Day-14 speed-tune: with_mask=True asks the mod to inline the cached
        # action_mask under obs["action_mask"], so the caller can skip a
        # separate /action_mask round-trip. The mod still serves /action_mask
        # for back-compat.
        params: list[str] = []
        if partial: params.append("partial=1")
        if with_mask: params.append("with_mask=1")
        query = "?" + "&".join(params) if params else ""
        return self._get_json(f"/observe{query}")

    def action_mask(self) -> dict[str, Any]:
        """Return the legal-action enumeration for the current state.

        Day-5 scope: combat phase only. Returns
            {phase, play_phase, round, actions: [{type, ...}]}
        where each action is either
            {"type": "play_card", "card_idx": int, "card_id": str, "cost": int,
             "target_type": str, "requires_target": bool,
             "legal_targets": [{"combat_id": int, "name": str}, ...]}
        or
            {"type": "end_turn"}
        """
        return self._get_json("/action_mask")

    def step(self, action: dict[str, Any], timeout: float = 30.0,
             with_obs: bool = False, partial: bool = False) -> dict[str, Any]:
        """POST an action to /step, await completion, return the response.

        Day-5 supported action types:
            {"type": "play_card", "card_idx": int, "target_combat_id": int|None}
            {"type": "end_turn"}

        ``with_obs=True`` (Day-14.10) asks the mod to inline the post-step
        observation under the ``"obs"`` key of the response — saves the agent
        a separate /observe round-trip on the next iteration. The inlined obs
        already includes ``action_mask`` (mirrors /observe?with_mask=1).

        Raises StepError on 4xx/5xx with the server's structured error payload
        (e.g. unplayable_reason, target_combat_id not found).
        """
        path = "/step"
        params: list[str] = []
        if with_obs:
            params.append("with_obs=1")
        if partial:
            params.append("partial=1")
        if params:
            path = path + "?" + "&".join(params)
        return self._post_json(path, action, timeout=timeout)

    # ---------- Day-9.1 selector toggle ----------

    def enable_selector(self) -> dict[str, Any]:
        """Push our ICardSelector — agent now intercepts every card-pick screen."""
        return self._post_json("/selector/enable", {})

    def disable_selector(self) -> dict[str, Any]:
        """Pop our ICardSelector — game's native UI handles card picks again."""
        return self._post_json("/selector/disable", {})

    # ---------- Day-9.2 fresh-run start ----------

    def start_run(self, character: str, ascension: int = 0, seed: str | None = None) -> dict[str, Any]:
        """Begin a fresh single-player run via RunManager.SetUpNewSinglePlayer.

        Parameters
        ----------
        character : one of {"IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"}
                    (case-insensitive — server normalizes).
        ascension : 0..10
        seed      : optional. If omitted, server generates a fresh "GYM<ticks>" seed.

        Errors
        ------
        409 if a run is already in progress (call CleanUp first via game UI).
        400 on unknown character / bad ascension.
        """
        payload: dict[str, Any] = {"character": character, "ascension": ascension}
        if seed is not None:
            payload["seed"] = seed
        return self._post_json("/start_run", payload, timeout=30.0)

    def treasure_open(self) -> dict[str, Any]:
        """Day-14: click the chest in a treasure room. No-op if already open."""
        return self._post_json("/step", {"type": "treasure_open"}, timeout=15.0)

    def treasure_pick(self, idx: int) -> dict[str, Any]:
        """Day-14: click a NTreasureRoomRelicHolder by idx (chest must be open first)."""
        return self._post_json("/step", {"type": "treasure_pick", "idx": int(idx)}, timeout=15.0)

    def treasure_leave(self) -> dict[str, Any]:
        """Day-14: click the proceed button to leave the treasure room."""
        return self._post_json("/step", {"type": "treasure_leave"}, timeout=15.0)

    def abandon_run(self) -> dict[str, Any]:
        """Tear down the currently active run via RunManager.CleanUp.

        No-op (returns ``{"ok": True, "was_active": False}``) if no run is
        active. Used to chain multiple :meth:`start_run` calls in tests like
        :mod:`sts2_gym.ascension_test`.
        """
        return self._post_json("/abandon_run", {}, timeout=15.0)

    # ---------- Day-13 Save / Restore ----------

    def save_run(self) -> dict[str, Any]:
        """Snapshot the current run as a SerializableRun JSON document.

        Returns a dict::

            {
                "ok": True,
                "schema_version": <int>,
                "ascension": <int>,
                "current_act_index": <int>,
                "rng_streams": <int>,
                "deck_size": <int>,
                "hp": <int>,
                "save": { ...SerializableRun JSON... },
            }

        Pass the ``save`` value back to :meth:`restore_run` to reload.

        Errors
        ------
        409 if no run is in progress, or if a combat round is currently active —
            mid-combat state isn't captured by SerializableRun (dev plan §2.1
            path (a) vs (b)). Save at room boundaries: map / event / reward /
            shop / rest.
        """
        return self._get_json("/save_run")

    def restore_run(self, save: dict[str, Any]) -> dict[str, Any]:
        """Reload a previously :meth:`save_run`-snapshotted SerializableRun.

        ``save`` is the ``"save"`` field from the :meth:`save_run` response.
        Any in-progress run is cleaned up before loading.
        """
        return self._post_json("/restore_run", {"save": save}, timeout=30.0)

    # ---------- Day-9.3 registry ----------

    def registry(self) -> dict[str, Any]:
        """Fetch the mod's card/monster/relic id → int registry.

        Includes ``game_version`` + ``content_hash`` so the py side can detect
        content drift between game patches.
        """
        return self._get_json("/registry")

    # ---------- Day-10.A non-combat phase actions ----------

    def choose_map_node(self, col: int, row: int) -> dict[str, Any]:
        """Pick the next map node by coordinate. Must be reachable from current location."""
        return self._post_json("/step", {"type": "choose_map_node", "col": col, "row": row}, timeout=30.0)

    def choose_event_option(self, option_idx: int) -> dict[str, Any]:
        """Pick option N on the current event screen. 0-indexed into observe.event.options."""
        return self._post_json("/step", {"type": "choose_event_option", "option_idx": option_idx}, timeout=30.0)

    def take_reward_item(self, idx: int) -> dict[str, Any]:
        """Claim reward item at index ``idx`` (see /observe.reward.items[*].idx).
        Card rewards open a sub-screen that runs through the ICardSelector
        (selector_active=true follows — resolve via select_pick / select_skip)."""
        return self._post_json("/step", {"type": "take_reward_item", "idx": idx}, timeout=15.0)

    def leave_reward_screen(self, force: bool = False) -> dict[str, Any]:
        """Click the post-combat reward screen's proceed button. Take any
        gold/potion/relic via take_reward_item first; card picks already
        route through ICardSelector (Day-8).

        ``force=True`` bypasses the "unclaimed items remaining" guard — used
        by the agent when it's tried to claim an item and the mod silently
        no-op'd (e.g. PotionReward when all 3 potion slots are full)."""
        payload: dict[str, Any] = {"type": "leave_reward_screen"}
        if force:
            payload["force"] = True
        return self._post_json("/step", payload, timeout=15.0)

    def proceed_after_game_over(self) -> dict[str, Any]:
        """Dismiss the game-over screen (returns to main menu)."""
        return self._post_json("/step", {"type": "proceed_after_game_over"}, timeout=15.0)

    def shop_buy(self, entry_idx: int) -> dict[str, Any]:
        """Buy a merchant entry by flat index (see /observe.shop.items[*].entry_idx)."""
        return self._post_json("/step", {"type": "shop_buy", "entry_idx": entry_idx}, timeout=30.0)

    def shop_leave(self) -> dict[str, Any]:
        """Leave the merchant room (UI click on proceed/back button)."""
        return self._post_json("/step", {"type": "shop_leave"}, timeout=15.0)

    def bundle_pick(self, idx: int) -> dict[str, Any]:
        """Pick a card bundle on NChooseABundleSelectionScreen (event outcome)."""
        return self._post_json("/step", {"type": "bundle_pick", "idx": idx}, timeout=15.0)

    def card_reward_pick(self, idx: int) -> dict[str, Any]:
        """Pick a card on NCardRewardSelectionScreen (sub-screen opened by
        clicking a CardReward NRewardButton on the parent NRewardsScreen).
        See /observe.card_reward_select.cards."""
        return self._post_json("/step", {"type": "card_reward_pick", "idx": idx}, timeout=15.0)

    def relic_pick(self, idx: int) -> dict[str, Any]:
        """Pick a relic on NChooseARelicSelection (Neow PRECARIOUS_SHEARS,
        treasure rooms, certain events). See /observe.relic_select.items."""
        return self._post_json("/step", {"type": "relic_pick", "idx": idx}, timeout=15.0)

    def rest_choose(self, option_idx: int) -> dict[str, Any]:
        """Choose a rest-site option (see /observe.rest.options[*].option_idx).

        Smith/Mend etc. that need a card pick will activate the ICardSelector
        afterward — agent must resolve via select_pick / select_confirm next.
        """
        return self._post_json("/step", {"type": "rest_choose", "option_idx": option_idx}, timeout=30.0)

    def rest_leave(self) -> dict[str, Any]:
        """Click the rest-room's proceed button after the option has resolved."""
        return self._post_json("/step", {"type": "rest_leave"}, timeout=15.0)

    def reset(
        self,
        *,
        encounter: str | None = None,
        rng_counters: dict[str, Any] | None = None,
        player_snapshot: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST a scenario reset (dev plan §2.2 Combat-level — Day-6.1).

        Parameters
        ----------
        encounter :
            If set, jump to this encounter via RunManager.EnterRoomDebug. Pre-condition:
            the game must already be in a run (Day-6 doesn't drive main-menu UI).
        rng_counters :
            If set, restore RunRngSet to these counter values BEFORE jumping to
            the encounter. Format mirrors what /observe returns under run.rng:
                {"seed": "MYSEED", "counters": {"shuffle": 12, "combat_targets": 7, ...}}
            The seed must match the current run's seed (RunRngSet doesn't support
            mid-run reseed; server returns 400 on mismatch).
        player_snapshot :
            If set, restore the full Player state (HP, deck, relics, potions,
            PlayerRng, RelicGrabBag, discovered-content lists) via the game's
            Player.SyncWithSerializedPlayer API. Pass the raw object from
            /observe.run.players[i] — schema is guaranteed round-trip compatible.

        Apply order on the server: player_snapshot → rng_counters → encounter.

        Returns
        -------
        Server response body. On success:
            {"ok": true, "player_restored"?: true, "rng_restored"?: true,
             "encounter"?: str, "phase_after"?: str}
        """
        payload: dict[str, Any] = {}
        if encounter is not None:
            payload["encounter"] = encounter
        if rng_counters is not None:
            payload["rng_counters"] = rng_counters
        if player_snapshot is not None:
            payload["player_snapshot"] = player_snapshot
        return self._post_json("/reset", payload, timeout=timeout)

    def snapshot_run_rng(self) -> dict[str, Any]:
        """Return the current run's RNG snapshot in the format /reset expects.

        Convenience method: /observe -> extract run.rng -> reshape as needed.
        """
        obs = self.observe()
        run = obs.get("run") or {}
        rng = run.get("rng") or {}
        # /observe gives {"seed": str, "counters": {...}} — same shape /reset wants.
        return {"seed": rng.get("seed"), "counters": rng.get("counters") or {}}

    def snapshot_player(self, player_index: int = 0) -> dict[str, Any]:
        """Return the player's full SerializablePlayer snapshot from /observe.

        The returned dict is directly round-trip compatible with /reset's
        ``player_snapshot`` parameter — the game's source-generated JSON
        context handles both directions of (de)serialization.
        """
        obs = self.observe()
        run = obs.get("run") or {}
        players = run.get("players") or []
        if not players:
            raise RuntimeError("no players in /observe.run — is the game actually in a run?")
        if player_index >= len(players):
            raise IndexError(f"player_index={player_index} out of range (have {len(players)} players)")
        return players[player_index]

    # ---------- utilities ----------

    def wait_until_ready(self, timeout_s: float = 30.0, poll_s: float = 0.5) -> None:
        """Block until ``/health`` responds, or raise TimeoutError.

        Useful for smoke scripts that launch the game and want to gate on the
        HTTP bridge actually being up.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                self.health()
                return
            except (URLError, OSError, ConnectionError):
                time.sleep(poll_s)
        raise TimeoutError(
            f"sts2gym HTTP bridge at {self.base} did not respond within {timeout_s}s "
            f"— is STS2 running with the mod loaded?"
        )
