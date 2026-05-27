"""Day-12: ``python -m sts2_gym.doctor`` — diagnostic checklist.

Walks through every prerequisite for the env to function, reports ✓ / ✗ /
warning per item, and prints a hint for each failure. Designed to be the
first command a new user runs after ``pip install``.

Checked items:
  1. STS2 install path (Steam library default + override via env)
  2. Mod directory exists inside the .app bundle
  3. mod files (.dll + manifest) deployed
  4. settings.save → mod_settings.mods_enabled = true
     (PlayerAgreedToModLoading bypass — dev plan §3.2)
  5. HTTP bridge reachable on the expected port
  6. /version protocol compatibility
  7. /registry has card/monster ids (i.e. mod actually initialized ModelDb)

Exit code 0 = all pass, 1 = at least one failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sts2_gym.client import DEFAULT_PORT, ModBridgeClient

# Default macOS paths. Windows / Linux are TODO — see fallback envs.
DEFAULT_MAC_INSTALL = Path(
    "/Users/Shared/SlayTheSpire2"
)  # Steam doesn't actually default here; user usually overrides.
DEFAULT_MAC_STEAM_INSTALL = Path(
    os.path.expanduser(
        "~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
    )
)
DEFAULT_MAC_USERDATA = Path(
    os.path.expanduser("~/Library/Application Support/SlayTheSpire2")
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""


def _resolve_install_path() -> Path | None:
    """Find STS2 install directory. Env override first, then Steam default."""
    override = os.environ.get("STS2_INSTALL")
    if override:
        p = Path(override)
        return p if p.exists() else None
    if DEFAULT_MAC_STEAM_INSTALL.exists():
        return DEFAULT_MAC_STEAM_INSTALL
    return None


def _mod_dir(install_path: Path) -> Path:
    """The deployed-mod path inside the .app bundle on macOS."""
    return install_path / "SlayTheSpire2.app/Contents/MacOS/mods/sts2gym"


def _resolve_settings_save() -> Path | None:
    """Find settings.save (per Steam user directory). Returns None if not found."""
    root = DEFAULT_MAC_USERDATA / "steam"
    if not root.exists():
        return None
    # Pick the most-recently-modified user dir; STS2 writes settings per-Steam-user
    candidates = sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / "settings.save").exists()),
        key=lambda p: (p / "settings.save").stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0] / "settings.save"


# ----- individual checks --------------------------------------------------

def check_install() -> Check:
    p = _resolve_install_path()
    if p is None:
        return Check(
            "STS2 install path", ok=False,
            detail="not found",
            hint="set STS2_INSTALL=/path/to/Slay the Spire 2 (the dir containing SlayTheSpire2.app)",
        )
    return Check("STS2 install path", ok=True, detail=str(p))


def check_mod_deployed(install_path: Path) -> Check:
    mod_dir = _mod_dir(install_path)
    if not mod_dir.exists():
        return Check(
            "Mod deployed", ok=False,
            detail=f"{mod_dir} missing",
            hint="run `sts2-gym/scripts/smoke_test.sh --no-game` from the project root to build + deploy",
        )
    dll = mod_dir / "sts2gym.dll"
    manifest = mod_dir / "sts2gym.json"
    if not dll.exists():
        return Check("Mod deployed", ok=False, detail="sts2gym.dll missing", hint="rebuild + redeploy")
    if not manifest.exists():
        return Check("Mod deployed", ok=False, detail="sts2gym.json missing", hint="rebuild + redeploy")
    return Check("Mod deployed", ok=True, detail=f"{dll.stat().st_size//1024} KB DLL + manifest")


def check_mods_enabled(settings_path: Path | None) -> Check:
    if settings_path is None:
        return Check(
            "mods_enabled flag", ok=False,
            detail="settings.save not found",
            hint=("launch STS2 once first so it creates ~/Library/Application Support/SlayTheSpire2/steam/<id>/settings.save"),
        )
    try:
        data = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return Check("mods_enabled flag", ok=False, detail=f"can't parse {settings_path}: {e}",
                     hint="check file permissions; if file is corrupt, delete it and let STS2 recreate")
    mod_settings = data.get("mod_settings") or {}
    enabled = mod_settings.get("mods_enabled")
    if enabled is True:
        return Check("mods_enabled flag", ok=True, detail=f"{settings_path.name} OK")
    return Check(
        "mods_enabled flag", ok=False,
        detail=f"mod_settings.mods_enabled = {enabled!r}",
        hint=("run `python -m sts2_gym.install --enable-mods` to patch it, OR launch STS2 and "
              "click 'Enable Mods' in settings"),
    )


def check_http(client: ModBridgeClient) -> Check:
    try:
        h = client.health()
    except Exception as e:
        return Check(
            "HTTP bridge /health", ok=False,
            detail=f"{e!r}",
            hint=f"is STS2 running with the mod loaded? Try `curl {client.base}/health`",
        )
    return Check("HTTP bridge /health", ok=True, detail=str(h))


def check_version(client: ModBridgeClient) -> Check:
    try:
        v = client.version()
    except Exception as e:
        return Check("/version", ok=False, detail=f"{e!r}", hint="rebuild + restart STS2")
    expected = 1  # protocol_version we know
    proto = v.get("protocol_version")
    if proto != expected:
        return Check(
            "/version", ok=False, detail=f"protocol_version={proto}, expected {expected}",
            hint="client and mod disagree on wire protocol — update both ends",
        )
    return Check("/version", ok=True, detail=f"mod={v.get('version')} protocol={proto}")


def check_registry(client: ModBridgeClient) -> Check:
    try:
        r = client.registry()
    except Exception as e:
        return Check("/registry", ok=False, detail=f"{e!r}",
                     hint="mod may have loaded but ModelDb didn't initialize — check Godot log for stack traces")
    counts = r.get("counts") or {}
    cards = counts.get("cards", 0)
    if cards < 50:
        return Check("/registry", ok=False, detail=f"only {cards} cards in registry",
                     hint="ModelDb wasn't fully populated — game version may have changed schema")
    return Check("/registry", ok=True,
                 detail=f"{counts.get('cards')} cards / {counts.get('monsters')} monsters / "
                        f"{counts.get('encounters')} encounters / game={r.get('game_version')}")


# ----- runner -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sts2_gym doctor — environment self-check")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-http", action="store_true",
                        help="Skip the HTTP-bridge checks (use when STS2 isn't running yet)")
    args = parser.parse_args(argv)

    checks: list[Check] = []

    inst = check_install()
    checks.append(inst)
    if inst.ok:
        checks.append(check_mod_deployed(Path(inst.detail)))

    checks.append(check_mods_enabled(_resolve_settings_save()))

    if not args.no_http:
        client = ModBridgeClient(port=args.port)
        h = check_http(client)
        checks.append(h)
        if h.ok:
            checks.append(check_version(client))
            checks.append(check_registry(client))

    # Print report
    print(f"{'='*70}")
    print(f" sts2_gym doctor — {len(checks)} checks")
    print(f"{'='*70}")
    for c in checks:
        glyph = "✓" if c.ok else "✗"
        print(f"  {glyph} {c.name:<25} {c.detail}")
        if not c.ok and c.hint:
            print(f"     → hint: {c.hint}")
    print()
    n_fail = sum(1 for c in checks if not c.ok)
    if n_fail == 0:
        print("All checks passed. STS2-Gym is ready to use.")
        return 0
    print(f"{n_fail} check(s) failed. See hints above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
