"""Day-14: VectorEnv for STS2-Gym.

Multi-instance training. **N envs ⇒ N OS processes** — STS2 is a per-process
singleton (``RunManager.Instance`` / ``CombatManager.Instance`` are global),
so a single Python process can't host two concurrent runs in different states.
See dev plan §2.7 for the constraint.

This module ships two compositions:

* :class:`STS2VectorEnv` — a thin wrapper that hides Gymnasium's
  ``SyncVectorEnv`` setup. You either pass a list of pre-launched
  :class:`~sts2_gym.process.GameProcess` handles, or a list of ports to spawn
  fresh instances on. Either way each underlying ``STS2CombatEnv`` is bound to
  exactly one game process.

* :func:`build_async_vector_env` — convenience around Gymnasium's
  ``AsyncVectorEnv`` for users who want subprocess-isolated env stepping. Each
  Python child process opens its own ModBridgeClient → independent game
  process. Cost: pickle round-trip on every step / reset.

Both compositions yield the standard gymnasium VectorEnv API
(``reset`` / ``step`` / ``close`` returning batched arrays).

**Setup checklist** (manual launch path):

  1. Build + deploy the mod: ``bash scripts/smoke_test.sh --no-game``.
  2. Pre-launch N STS2 instances, each with a unique ``STS2GYM_PORT`` env var.
     The simplest pattern on macOS::

         for port in 7777 7778 7779 7780; do
             STS2GYM_PORT=$port \\
             STS2GYM_PORT_LOCKFILE=/tmp/sts2_gym_$port.port \\
             open -na "Slay the Spire 2"
         done

     ``open -na`` opens a *new* instance instead of reusing the existing one.
  3. From Python::

         from sts2_gym import STS2VectorEnv
         venv = STS2VectorEnv.from_ports([7777, 7778, 7779, 7780],
                                          character="IRONCLAD")
         obs, info = venv.reset()
         obs, reward, terminated, truncated, info = venv.step(action_batch)

Auto-spawn path::

    from sts2_gym.process import GameProcess
    procs = [GameProcess.spawn(7777 + i) for i in range(4)]
    venv = STS2VectorEnv(procs, character="IRONCLAD")
    # ...
    venv.close()  # also closes each owned GameProcess
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

import gymnasium as gym
from gymnasium.vector import SyncVectorEnv

from sts2_gym.env import STS2CombatEnv
from sts2_gym.process import GameProcess


def _env_factory(
    proc: GameProcess,
    character: str | None,
    ascension: int,
    run_seed: str | None,
    encounter: str | None,
    partial_obs: bool,
    max_steps: int,
    reward_mode: str,
    use_registry: bool,
) -> Callable[[], STS2CombatEnv]:
    """Returns a zero-arg thunk that constructs an STS2CombatEnv bound to ``proc``."""
    def make() -> STS2CombatEnv:
        return STS2CombatEnv(
            encounter=encounter,
            character=character,
            ascension=ascension,
            run_seed=run_seed,
            client=proc.client,
            max_steps=max_steps,
            reward_mode=reward_mode,
            partial_obs=partial_obs,
            use_registry=use_registry,
        )
    return make


class STS2VectorEnv(SyncVectorEnv):
    """Synchronous vector env over N pre-bound :class:`GameProcess` handles.

    Subclasses Gymnasium's ``SyncVectorEnv`` so it returns the standard batched
    ``(obs, reward, terminated, truncated, info)`` tuple — drop-in compatible
    with PPO trainers, evaluation loops, etc.

    Important: per-env ``run_seed`` is constant across resets (we don't permute
    it). If you want varied seeds per env, pass ``run_seeds`` as a list of the
    same length as ``processes``.

    Parameters
    ----------
    processes :
        List of :class:`GameProcess` handles. Length = num_envs.
    character / ascension / run_seed / encounter / partial_obs / max_steps / reward_mode / use_registry :
        Per-env arguments forwarded to each underlying ``STS2CombatEnv``. Pass
        a list (one per env) to vary them, or a scalar to broadcast.
    """

    def __init__(
        self,
        processes: Sequence[GameProcess],
        *,
        character: str | list[str | None] | None = None,
        ascension: int | list[int] = 0,
        run_seed: str | list[str | None] | None = None,
        encounter: str | list[str | None] | None = None,
        partial_obs: bool | list[bool] = False,
        max_steps: int | list[int] = 200,
        reward_mode: str | list[str] = "sparse",
        use_registry: bool | list[bool] = True,
    ):
        if not processes:
            raise ValueError("STS2VectorEnv requires at least one GameProcess")
        n = len(processes)
        self._processes = list(processes)

        def broadcast(x: Any, name: str) -> list[Any]:
            if isinstance(x, list):
                if len(x) != n:
                    raise ValueError(f"{name} list length {len(x)} != num_envs {n}")
                return x
            return [x] * n

        chars = broadcast(character, "character")
        ascs = broadcast(ascension, "ascension")
        seeds = broadcast(run_seed, "run_seed")
        encs = broadcast(encounter, "encounter")
        pobs = broadcast(partial_obs, "partial_obs")
        msteps = broadcast(max_steps, "max_steps")
        rmode = broadcast(reward_mode, "reward_mode")
        ureg = broadcast(use_registry, "use_registry")

        fns = [
            _env_factory(processes[i], chars[i], ascs[i], seeds[i], encs[i],
                         pobs[i], msteps[i], rmode[i], ureg[i])
            for i in range(n)
        ]
        super().__init__(fns)

    @classmethod
    def from_ports(cls, ports: Sequence[int], **kwargs: Any) -> "STS2VectorEnv":
        """Convenience: build N GameProcess handles in `owns_process=False` mode.

        The caller is expected to have pre-launched N STS2 instances on those
        ports. We don't health-check at construction time — Gymnasium will
        attempt the first ``observe()`` on the first ``reset()`` and surface any
        failure there.
        """
        procs = [GameProcess(port=p) for p in ports]
        return cls(procs, **kwargs)

    @classmethod
    def spawn(cls, num_envs: int, base_port: int = 7777, **kwargs: Any) -> "STS2VectorEnv":
        """Auto-spawn N STS2 instances at ``[base_port, base_port + N)``.

        Each spawned process is owned by its GameProcess handle and will be
        terminated when :meth:`close` runs. Health-checks each before
        constructing the env.
        """
        procs = [GameProcess.spawn(base_port + i) for i in range(num_envs)]
        return cls(procs, **kwargs)

    def close(self, **kwargs: Any) -> None:
        super().close(**kwargs)
        for proc in self._processes:
            try:
                proc.close()
            except Exception:
                # Don't let a single bad-actor process keep us from cleaning the rest.
                pass


def build_async_vector_env(
    processes: Sequence[GameProcess],
    *,
    character: str | None = None,
    ascension: int = 0,
    run_seed: str | None = None,
    encounter: str | None = None,
    partial_obs: bool = False,
    max_steps: int = 200,
    reward_mode: str = "sparse",
    use_registry: bool = True,
) -> gym.vector.AsyncVectorEnv:
    """Build an AsyncVectorEnv over the same processes.

    Each Python child subprocess holds one ``STS2CombatEnv``. Useful when you
    want true parallelism for env stepping (e.g. when an env step is dominated
    by HTTP latency). Pickle cost adds up to ~0.5ms per step — not worth it
    unless step time is already >> 10ms.

    Note: ``ModBridgeClient`` uses stdlib urllib so pickling is straightforward,
    but a fresh client is constructed in the child by re-opening the same port.
    """
    from gymnasium.vector import AsyncVectorEnv
    fns = [
        _env_factory(p, character, ascension, run_seed, encounter,
                     partial_obs, max_steps, reward_mode, use_registry)
        for p in processes
    ]
    return AsyncVectorEnv(fns)
