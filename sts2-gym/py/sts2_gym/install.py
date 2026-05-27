"""Day-12: ``python -m sts2_gym.install`` — automated setup helper.

What it does:
  * --enable-mods  : Patch settings.save's ``mod_settings.mods_enabled = true``,
    bypassing the in-game "Enable Mods" toggle. **Required before mods load**
    — without this the mod stays disabled even when deployed to the mods
    directory (dev plan §3.2: this is the silent-deadlock killer).
  * --status       : Show the current value without changing anything.
  * --revert       : Set mods_enabled back to false.

Backup: any change writes the original to ``<settings.save>.sts2gym_backup``
first. If something breaks STS2's settings load, you can restore the backup
and the game will recreate settings.save with defaults.

Run :mod:`sts2_gym.doctor` afterward to verify.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from sts2_gym.doctor import _resolve_settings_save


def _read(settings_path: Path) -> dict:
    try:
        return json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"[install] ✗ can't parse {settings_path}: {e}")


def _backup(settings_path: Path) -> Path:
    backup = settings_path.with_suffix(settings_path.suffix + ".sts2gym_backup")
    if not backup.exists():
        shutil.copy2(settings_path, backup)
        print(f"[install] backup → {backup}")
    return backup


def _write(settings_path: Path, data: dict) -> None:
    _backup(settings_path)
    settings_path.write_text(json.dumps(data, indent=4, sort_keys=True))


def cmd_status(settings_path: Path) -> int:
    data = _read(settings_path)
    val = (data.get("mod_settings") or {}).get("mods_enabled")
    print(f"[install] settings.save: {settings_path}")
    print(f"[install] mod_settings.mods_enabled = {val!r}")
    return 0


def cmd_enable(settings_path: Path) -> int:
    data = _read(settings_path)
    ms = data.setdefault("mod_settings", {})
    cur = ms.get("mods_enabled")
    if cur is True:
        print(f"[install] mod_settings.mods_enabled already true — no change")
        return 0
    ms["mods_enabled"] = True
    _write(settings_path, data)
    print(f"[install] ✓ patched mod_settings.mods_enabled: {cur!r} → True")
    print("[install] launch STS2 to load the mod; then run `python -m sts2_gym.doctor` to verify")
    return 0


def cmd_revert(settings_path: Path) -> int:
    data = _read(settings_path)
    ms = data.setdefault("mod_settings", {})
    if ms.get("mods_enabled") is False:
        print("[install] already false — no change")
        return 0
    ms["mods_enabled"] = False
    _write(settings_path, data)
    print("[install] ✓ set mod_settings.mods_enabled = False")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sts2_gym install helper")
    sub = parser.add_mutually_exclusive_group(required=True)
    sub.add_argument("--enable-mods", action="store_true",
                     help="set mod_settings.mods_enabled = true (the consent bypass)")
    sub.add_argument("--status", action="store_true", help="show current value")
    sub.add_argument("--revert", action="store_true", help="set mod_settings.mods_enabled = false")
    parser.add_argument("--settings", type=Path, default=None,
                        help="override settings.save path (autodetected by default)")
    args = parser.parse_args(argv)

    settings = args.settings or _resolve_settings_save()
    if settings is None or not settings.exists():
        print("[install] ✗ settings.save not found.")
        print("[install]   Launch STS2 once first to create it. macOS default path:")
        print("[install]   ~/Library/Application Support/SlayTheSpire2/steam/<steamid>/settings.save")
        return 1

    if args.status:
        return cmd_status(settings)
    if args.enable_mods:
        return cmd_enable(settings)
    if args.revert:
        return cmd_revert(settings)
    parser.error("must pick one mode")


if __name__ == "__main__":
    sys.exit(main())
