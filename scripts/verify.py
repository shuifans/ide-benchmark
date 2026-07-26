"""L0 客观检查：在 work/ 内执行任务的 verifier 命令，产出 score.json 骨架。

用法：
  python scripts/verify.py <run_id>
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from common import (
    RESULTS_RUNS_DIR, RESULTS_SCORES_DIR, RUNS_DIR, TASKS_DIR,
    dump_json, load_json, load_task, validate, eprint,
)

PYTEST_SUMMARY_RE = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "error": re.compile(r"(\d+)\s+error"),
}


def parse_pytest_output(output: str) -> tuple[int, int] | None:
    """解析 pytest 摘要，返回 (passed, total)；非 pytest 输出返回 None。"""
    counts = {}
    for key, pattern in PYTEST_SUMMARY_RE.items():
        m = pattern.search(output)
        counts[key] = int(m.group(1)) if m else 0
    if counts["passed"] + counts["failed"] + counts["error"] == 0:
        return None
    passed = counts["passed"]
    total = passed + counts["failed"] + counts["error"]
    return passed, total


def verify(run_id: str) -> Path:
    run = load_json(RESULTS_RUNS_DIR / f"{run_id}.run.json")
    work_dir = RUNS_DIR / run_id / "work"
    if not work_dir.is_dir():
        raise FileNotFoundError(f"work 目录不存在: {work_dir}")

    task = load_task(run["task_id"])
    verifier = task.get("verifier") or {}
    command = verifier.get("command")
    if not command:
        raise RuntimeError(f"任务 {run['task_id']} 未配置 verifier.command")

    if verifier.get("copy_into_work", True):
        src = Path(task["_dir"]) / "verifier"
        dst = work_dir / "verifier"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)

    timeout = int(task.get("timeout_s", 600))
    try:
        proc = subprocess.run(
            command, shell=True, cwd=work_dir, capture_output=True,
            text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = f"(verifier 超时 {timeout}s)\n" + (exc.stdout or "")
        exit_code = -1

    parsed = parse_pytest_output(output)
    if parsed is not None:
        passed, total = parsed
    else:
        # 非 pytest verifier：按退出码给 0/1
        passed, total = (1, 1) if exit_code == 0 else (0, 1)

    score = round(passed / total * 100, 1) if total else 0.0
    score_doc = {
        "run_id": run_id,
        "task_id": run["task_id"],
        "verifier": {
            "passed": passed,
            "total": total,
            "score": score,
            "exit_code": exit_code,
            "log_excerpt": output[-4000:],
        },
    }
    errors = validate(score_doc, "score")
    if errors:
        raise RuntimeError("score.json 未通过 schema 校验: " + "; ".join(errors))

    out = RESULTS_SCORES_DIR / f"{run_id}.score.json"
    dump_json(score_doc, out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="执行 verifier 客观检查")
    ap.add_argument("run_id")
    args = ap.parse_args()
    try:
        out = verify(args.run_id)
    except (FileNotFoundError, RuntimeError) as exc:
        eprint(f"verify 失败: {exc}")
        return 1
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
