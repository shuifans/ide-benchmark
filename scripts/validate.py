"""schema 校验器。

用法：
  python scripts/validate.py --schema run results/runs/<run_id>.run.json
  python scripts/validate.py --schema manifest runs/<run_id>/manifest.json
  python scripts/validate.py --all          # 校验 results/ 下全部 run/score
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import RESULTS_RUNS_DIR, RESULTS_SCORES_DIR, load_json, validate, eprint


def check_file(path: Path, schema_name: str) -> bool:
    try:
        data = load_json(path)
    except (OSError, ValueError) as exc:
        eprint(f"[FAIL] {path}: 无法解析 JSON ({exc})")
        return False
    errors = validate(data, schema_name)
    if errors:
        eprint(f"[FAIL] {path} (schema={schema_name})")
        for e in errors:
            eprint(f"  - {e}")
        return False
    print(f"[OK] {path} (schema={schema_name})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="bench schema 校验器")
    ap.add_argument("--schema", choices=["manifest", "run", "score"], help="对单个文件使用的 schema")
    ap.add_argument("files", nargs="*", help="待校验文件")
    ap.add_argument("--all", action="store_true", help="校验 results/ 下全部 run/score")
    args = ap.parse_args()

    ok = True
    if args.all:
        targets = [
            (RESULTS_RUNS_DIR, "*.run.json", "run"),
            (RESULTS_SCORES_DIR, "*.score.json", "score"),
        ]
        found = 0
        for d, glob, schema_name in targets:
            for f in sorted(d.glob(glob)):
                found += 1
                ok = check_file(f, schema_name) and ok
        if found == 0:
            eprint("results/ 下没有找到任何 run/score 文件")
            return 2
        return 0 if ok else 1

    if not args.schema or not args.files:
        ap.error("需要 --schema <name> <file...> 或 --all")

    for f in args.files:
        ok = check_file(Path(f), args.schema) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
