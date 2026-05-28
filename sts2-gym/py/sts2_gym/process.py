"""Day-14: per-instance STS2 process management.

Dev plan §2.7 / §11 P1 mandates that VectorEnv = N independent OS processes —
``RunManager.Instance`` is a per-process singleton, so two concurrent envs in the
same OS process would smash each other's state via global accessors.

This module provides :class:`GameProcess`, a thin wrapper around one STS2
instance: port + lockfile + health check + optional ``spawn()`` of the binary
directly. Used as a building block by :class:`sts2_gym.vector_env.STS2VectorEnv`,
and useful on its own when you'd rather pre-launch STS2 via Steam and just have
Python connect to it.

Typical workflows:

* **Manual launch (typical research)**: user pre-launches STS2 via Steam with
  ``STS2GYM_PORT=7778`` set in the environment; we construct
  ``GameProcess(port=7778, owns_process=False)`` and ``wait_ready()`` to
  health-check.
* **Auto launch (CI / batch jobs)**: ``GameProcess.spawn(port=7778)``  invokes
  the binary directly via ``subprocess.Popen``, waits for ``/health``, returns
  a ready-to-use handle. ``terminate()`` kills the process on exit.

Limitations (documented for the user, not bugs):

* On macOS, multiple instances share the same Godot user-data dir
  ``~/Library/Application Support/SlayTheSpire2/``. ``shouldSave=false`` in the
  mod's ``/start_run`` path keeps save-file thrash to a minimum, but
  ``settings.save`` is still shared — every instance reads the same
  ``PlayerAgreedToModLoading`` value (which is fine, we want True everywhere).
* Spawn does NOT go through Steam, so Steam-specific features (cloud saves,
  achievements) are inert during automated runs. Use ``owns_process=False`` and
  pre-launch via Steam if you need those.
* The mod writes its bound port to a single lockfile at
  ``/tmp/sts2_gym.port`` by default. For multi-instance, set
  ``STS2GYM_PORT_LOCKFILE`` per instance (handled automatically by ``spawn()``).
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sts2_gym.client import DEFAULT_HOST, ModBridgeClient


def default_install_path() -> Path | None:
    """Best-effort autodetect of the STS2 .app bundle on macOS."""
    if platform.system() != "Darwin":
        return None
    candidate = Path.home() / "Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
    return candidate if candidate.is_dir() else None


def find_binary(install_path: Path | None = None) -> Path | None:
    """Return the path to the STS2 executable, or None if not found."""
    install_path = install_path or default_install_path()
    if install_path is None:
        return None
    if platform.system() == "Darwin":
        binary = install_path / "SlayTheSpire2.app" / "Contents" / "MacOS" / "SlayTheSpire2"
        return binary if binary.is_file() else None
    # Linux / Windows paths TBD — STS2-Gym hasn't been validated on those yet.
    return None


@dataclass
class GameProcess:
    """One STS2 instance accessible at ``(host, port)`` with an attached :class:`ModBridgeClient`.

    Parameters
    ----------
    port :
        TCP port the mod's HTTP listener is bound to (or will bind to, if
        ``owns_process=True``).
    host :
        Defaults to 127.0.0.1.
    owns_process :
        True when this object spawned the binary and should kill it on
        :meth:`close`. False when the user pre-launched STS2 via Steam.
    user_data_dir :
        Advisory — for future per-instance user-data isolation. Currently
        ignored on macOS (Godot's OS_USER_DATA_DIR is fixed to
        ``~/Library/Application Support/SlayTheSpire2/`` and isn't env-var
        configurable without source patches).
    port_lockfile :
        Path the mod writes the bound port to. Per-instance file when spawning
        multiple processes.
    extra_env :
        Additional environment variables to pass to ``subprocess.Popen``.
    """

    port: int
    host: str = DEFAULT_HOST
    owns_process: bool = False
    user_data_dir: Path | None = None
    port_lockfile: Path = field(default_factory=lambda: Path("/tmp/sts2_gym.port"))
    extra_env: dict[str, str] = field(default_factory=dict)

    _popen: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _client: ModBridgeClient | None = field(default=None, init=False, repr=False)

    @property
    def client(self) -> ModBridgeClient:
        """Lazy-initialized client at this instance's (host, port)."""
        if self._client is None:
            self._client = ModBridgeClient(port=self.port, host=self.host)
        return self._client

    @property
    def is_running(self) -> bool:
        """True if we own the process and it hasn't exited yet. Always True for
        externally-managed processes (we can't tell without a /health probe —
        use :meth:`is_healthy` for that)."""
        if not self.owns_process:
            return True
        return self._popen is not None and self._popen.poll() is None

    def is_healthy(self, timeout: float = 1.5) -> bool:
        """Probe ``/health`` with a short timeout. Cheap, non-throwing."""
        try:
            self.client.health()
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- spawn

    @classmethod
    def spawn(
        cls,
        port: int,
        *,
        install_path: Path | None = None,
        port_lockfile: Path | None = None,
        user_data_dir: Path | None = None,
        extra_env: dict[str, str] | None = None,
        wait: float | None = 30.0,
    ) -> "GameProcess":
        """Launch a fresh STS2 process directly via subprocess.Popen.

        The mod's port + lockfile are set via env vars. Returns a ``GameProcess``
        with ``owns_process=True`` so :meth:`close` will kill it.

        Pass ``wait=None`` to skip the readiness probe (caller drives ``/health``
        themselves). Default 30s timeout matches typical STS2 cold-boot time.
        """
        binary = find_binary(install_path)
        if binary is None:
            raise FileNotFoundError(
                "STS2 binary not found. Pass install_path=... or install STS2 via Steam at the default path."
            )
        if port_lockfile is None:
            port_lockfile = Path(f"/tmp/sts2_gym_{port}.port")

        env = os.environ.copy()
        env["STS2GYM_PORT"] = str(port)
        env["STS2GYM_PORT_LOCKFILE"] = str(port_lockfile)
        env["STS2GYM_HOST"] = DEFAULT_HOST
        if extra_env:
            env.update(extra_env)

        # If the lockfile exists from a previous run, remove it so we don't
        # mistake stale info for a fresh boot.
        try:
            port_lockfile.unlink()
        except FileNotFoundError:
            pass

        popen = subprocess.Popen(
            [str(binary)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # so SIGINT to the parent doesn't propagate
        )
        proc = cls(
            port=port,
            owns_process=True,
            user_data_dir=user_data_dir,
            port_lockfile=port_lockfile,
            extra_env=extra_env or {},
        )
        proc._popen = popen
        if wait is not None:
            proc.wait_ready(timeout=wait)
        return proc

    # ---------------------------------------------------------- wait_ready

    def wait_ready(self, timeout: float = 30.0, interval: float = 0.5) -> None:
        """Block until ``/health`` returns 200 or ``timeout`` elapses."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self.owns_process and self._popen is not None and self._popen.poll() is not None:
                raise RuntimeError(
                    f"STS2 process exited with code {self._popen.returncode} before /health responded; "
                    "check godot.log under ~/Library/Application Support/SlayTheSpire2/logs/"
                )
            try:
                self.client.health()
                return
            except Exception as e:
                last_err = e
                time.sleep(interval)
        raise TimeoutError(
            f"STS2 instance at {self.host}:{self.port} not ready after {timeout}s "
            f"(last error: {last_err!r})"
        )

    # -------------------------------------------------------------- close

    def close(self, grace: float = 3.0) -> None:
        """Kill the owned process (no-op if owns_process=False). Idempotent."""
        if not self.owns_process or self._popen is None:
            return
        if self._popen.poll() is not None:
            self._popen = None
            return
        try:
            self._popen.terminate()
            try:
                self._popen.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self._popen.kill()
                self._popen.wait(timeout=grace)
        finally:
            self._popen = None
            # Tidy up our lockfile so a future spawn() on the same port starts clean.
            try:
                self.port_lockfile.unlink()
            except FileNotFoundError:
                pass

    # --------------------------------------------------- context manager

    def __enter__(self) -> "GameProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def kill_running_instances() -> int:
    """Best-effort SIGTERM for any orphaned STS2 processes (macOS).

    Returns the number of processes signalled. Useful in CI cleanup hooks where
    a previous test crashed before close()'ing its GameProcess.
    """
    if platform.system() != "Darwin":
        return 0
    try:
        out = subprocess.check_output(["pgrep", "-f", "SlayTheSpire2"], text=True).strip()
    except subprocess.CalledProcessError:
        return 0
    pids = [int(p) for p in out.split() if p]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    return len(pids)
