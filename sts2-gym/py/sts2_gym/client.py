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

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

    # ---------- HTTP primitives ----------

    def _get_json(self, path: str) -> dict[str, Any]:
        with urlopen(f"{self.base}{path}", timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """POST JSON, return parsed response. On non-2xx, raises StepError with parsed body."""
        body_bytes = json.dumps(payload).encode("utf-8")
        req = Request(
            f"{self.base}{path}",
            data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout if timeout is not None else self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            # Server returned 4xx/5xx — try to parse body and raise rich error.
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                err_json = json.loads(err_body)
            except (json.JSONDecodeError, ValueError):
                err_json = {"raw": err_body}
            raise StepError(status=e.code, payload=err_json) from None

    # ---------- endpoint wrappers ----------

    def health(self) -> dict[str, Any]:
        """Return the mod's liveness payload (mod id, version, protocol_version, port)."""
        return self._get_json("/health")

    def version(self) -> dict[str, Any]:
        return self._get_json("/version")

    def observe(self, partial: bool = False) -> dict[str, Any]:
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
        query = "?partial=1" if partial else ""
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

    def step(self, action: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """POST an action to /step, await completion, return the response.

        Day-5 supported action types:
            {"type": "play_card", "card_idx": int, "target_combat_id": int|None}
            {"type": "end_turn"}

        Raises StepError on 4xx/5xx with the server's structured error payload
        (e.g. unplayable_reason, target_combat_id not found).
        """
        return self._post_json("/step", action, timeout=timeout)

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
