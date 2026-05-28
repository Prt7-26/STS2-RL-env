"""Day-14: emit ``docs/schemas/*.schema.json`` from :mod:`sts2_gym.schemas`.

Usage::

    cd sts2-gym/py
    python -m sts2_gym.gen_schemas                    # writes to ../docs/schemas/
    python -m sts2_gym.gen_schemas --out /tmp/out      # custom output dir
    python -m sts2_gym.gen_schemas --check             # CI mode: non-zero exit if
                                                       #   on-disk schemas drift from
                                                       #   the source-of-truth module.

CI integration: hook ``--check`` into a pre-commit / pre-push step to keep
``docs/schemas/`` in sync with ``schemas.py``. The source-of-truth is the Python
module; the JSON files are derived artifacts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sts2_gym.schemas import ALL_SCHEMAS


def _serialize(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def _default_out_dir() -> Path:
    # sts2-gym/py/sts2_gym/ -> sts2-gym/docs/schemas/
    return Path(__file__).resolve().parent.parent.parent / "docs" / "schemas"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate JSON Schema files from sts2_gym.schemas")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: sts2-gym/docs/schemas/)")
    parser.add_argument("--check", action="store_true",
                        help="don't write; exit non-zero if on-disk files differ from source-of-truth")
    args = parser.parse_args(argv)

    out_dir = args.out or _default_out_dir()
    if not args.check:
        out_dir.mkdir(parents=True, exist_ok=True)

    drift: list[str] = []
    for name, schema in ALL_SCHEMAS.items():
        target = out_dir / f"{name}.schema.json"
        new_body = _serialize(schema)
        if args.check:
            if not target.exists():
                drift.append(f"missing: {target}")
                continue
            old_body = target.read_text(encoding="utf-8")
            if old_body != new_body:
                drift.append(f"out-of-date: {target}")
            continue
        target.write_text(new_body, encoding="utf-8")
        print(f"[gen_schemas] wrote {target.relative_to(target.parents[2]) if target.is_relative_to(target.parents[2]) else target} ({len(new_body)} bytes)")

    if args.check:
        if drift:
            print("[gen_schemas] schemas out of sync — run `python -m sts2_gym.gen_schemas`:")
            for d in drift:
                print(f"  - {d}")
            return 1
        print(f"[gen_schemas] ✓ {len(ALL_SCHEMAS)} schema files match source-of-truth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
