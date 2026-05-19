# sts2-gym (Python side)

Pre-MVP HTTP client for the in-game STS2-Gym mod.

## Layout

```
py/
├── pyproject.toml
└── sts2_gym/
    ├── __init__.py      # exposes ModBridgeClient
    ├── client.py        # stdlib urllib + json HTTP client
    └── probe.py         # `python -m sts2_gym.probe` smoke test
```

## Day-3 quick start

```bash
cd sts2-gym/py

# Either run-in-place (no install)…
python -m sts2_gym.probe

# …or install editable
pip install -e .
python -m sts2_gym.probe
```

Expected output (game running, in a run, mid-combat):
```
[probe] Port lockfile /tmp/sts2_gym.port -> 7777
[probe] Targeting http://127.0.0.1:7777
[probe] ✓ /health   -> {'status': 'ok', 'mod': 'sts2gym', 'version': '0.0.1', 'protocol_version': 1, 'port': 7777}
[probe] ✓ /version  -> {'mod': 'sts2gym', 'version': '0.0.1', 'protocol_version': 1}
[probe] ✓ /observe  -> phase='combat' in_run=True snapshot_age_ms=42
[probe]   schema_version=16
[probe]   ascension=0 game_mode='Standard' act=0
[probe]   ...
[probe]   combat: encounter='RUBY_RAIDERS_NORMAL' round=3 side=Player play_phase=True enemies=3
```

## Endpoints

| Endpoint | Returns | Notes |
|---|---|---|
| `GET /health` | `{status, mod, version, protocol_version, port}` | liveness probe |
| `GET /version` | `{mod, version, protocol_version}` | protocol handshake |
| `GET /observe` | `{phase, in_run, snapshot_age_ms, run?, combat?}` | snapshot, cached on game events |

Snapshot age is patched at response time, so `snapshot_age_ms` is accurate when the client reads it. The cache is refreshed by the mod on `RunStarted` / `CombatSetUp` / `TurnStarted` / `TurnEnded` events.

## What's NOT here yet

- `gymnasium.Env` wrapper (Day 4)
- Action endpoints / step API (Day 5)
- Full mid-combat `SerializableCombatState` (Day 4 — currently just minimal combat extension)
- Phase enum coverage of all 12 phases (Day 4 — currently 7 phases)
- ScenarioInjector (Day 6 / P0 milestone)
- LLM action parser (P0)

## Port selection

The mod reads `STS2GYM_PORT` env var at startup (default 7777). The client tries, in order:
1. The `port=` constructor argument
2. The `STS2GYM_PORT` env var
3. The `/tmp/sts2_gym.port` lockfile written by the mod
4. Default 7777

This three-way fallback supports both single-instance dev and (eventually) VectorEnv multi-process.

## Dev plan reference

- §2.6 Transport — HTTP server starts in mod, port from env, lockfile written
- §2.1 Serializer — `/observe` uses `RunManager.ToSave()` (path a, free reuse)
- §5.1 Protocol version — `protocol_version` field on every endpoint
- §11 P0 — Mod HTTP server + /observe milestone ✓
