"""L1 过程探测器：从归一化事件流（events.jsonl）提取 harness 过程能力信号。

用法：
  python scripts/process_metrics.py <run_id>

信号（写入 run.json process 段 + score.json process_signals 段）：
  planning                 首个编辑前是否存在计划（计划类工具调用或枚举式计划文本）
  plan_items/plan_completed 计划条目数与完成数（计划类工具可精确统计时）
  scope_adherence          实际编辑文件中落在计划声明范围内的比例（计划未声明文件时为 null）
  self_test_after_last_edit 最后一次编辑之后是否执行过测试/构建/含 assert 的验证脚本
  self_test_kind           framework（测试框架）| adhoc（内联脚本）| null
  retry_pattern_max        同一失败命令无变化连续重试的最大次数
  failed_tool_calls        失败的工具调用次数
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from common import (
    RESULTS_RUNS_DIR, RESULTS_SCORES_DIR, RUNS_DIR, dump_json, load_json, eprint,
)

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"
sys.path.insert(0, str(ADAPTERS_DIR))

from base import ADHOC_TEST_RE, ASSERT_HINT_RE, TEST_COMMAND_RE, load_events  # noqa: E402

PLAN_TEXT_MIN_LEN = 200
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s+\S", re.MULTILINE)
FILE_PATH_RE = re.compile(
    r"[\w.\-/\\]+\.(?:py|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|hpp|cs|rb|php|vue|"
    r"json|ya?ml|toml|md|txt|sh|bat|sql|html|css)\b"
)


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _plan_texts(events: list[dict]) -> list[str]:
    """收集计划相关内容：计划类工具输入 + 首个编辑前的枚举式计划消息。"""
    texts = []
    for ev in events:
        if ev.get("type") == "tool_call" and ev.get("tool_class") == "plan":
            texts.append(str(ev.get("plan_input") or ev.get("name") or ""))
    return texts


def compute_signals(events: list[dict]) -> dict:
    edits = [i for i, ev in enumerate(events)
             if ev.get("type") == "tool_call" and ev.get("tool_class") == "edit"]
    first_edit_idx = edits[0] if edits else None
    last_edit_idx = edits[-1] if edits else None

    # --- planning ---
    plan_calls = [ev for ev in events
                  if ev.get("type") == "tool_call" and ev.get("tool_class") == "plan"]
    planning = bool(plan_calls)
    if not planning and first_edit_idx is not None:
        for ev in events[:first_edit_idx]:
            if ev.get("type") == "assistant_message":
                text = ev.get("text") or ""
                if len(text) >= PLAN_TEXT_MIN_LEN and len(LIST_ITEM_RE.findall(text)) >= 3:
                    planning = True
                    break

    # --- plan items / completed（取最后一次含 todos 的快照）---
    plan_items = plan_completed = 0
    for ev in plan_calls:
        todos = (ev.get("plan_input") or {}).get("todos")
        if isinstance(todos, list) and todos:
            plan_items = len(todos)
            plan_completed = sum(
                1 for t in todos
                if isinstance(t, dict) and str(t.get("status", "")).lower() in ("completed", "done")
            )

    # --- scope adherence ---
    declared_files: set[str] = set()
    for text in _plan_texts(events):
        for m in FILE_PATH_RE.finditer(text):
            declared_files.add(_basename(m.group(0)))
    if not declared_files and first_edit_idx is not None:
        for ev in events[:first_edit_idx]:
            if ev.get("type") == "assistant_message" and ev.get("text"):
                for m in FILE_PATH_RE.finditer(ev["text"]):
                    declared_files.add(_basename(m.group(0)))
    edited_files = {
        _basename(f)
        for i in edits
        for f in (events[i].get("files") or [])
    }
    if declared_files and edited_files:
        in_scope = len(edited_files & declared_files)
        scope_adherence = round(in_scope / len(edited_files), 3)
    else:
        scope_adherence = None

    # --- self test after last edit（框架命令或含 assert 的解释器内联脚本）---
    self_test = False
    self_test_kind = None
    if last_edit_idx is not None:
        for ev in events[last_edit_idx + 1:]:
            if ev.get("type") == "tool_call" and ev.get("tool_class") == "command":
                cmd = ev.get("command") or ""
                if TEST_COMMAND_RE.search(cmd):
                    self_test, self_test_kind = True, "framework"
                    break
                if ADHOC_TEST_RE.search(cmd) and ASSERT_HINT_RE.search(cmd):
                    self_test, self_test_kind = True, "adhoc"
                    break

    # --- retry pattern（同一命令失败后无变化连续重试）---
    retry_max = 0
    last_cmd = None
    last_failed = False
    streak = 0
    for ev in events:
        if ev.get("type") == "tool_call" and ev.get("tool_class") == "command":
            cmd = (ev.get("command") or "").strip()
            if cmd and cmd == last_cmd and last_failed:
                streak += 1
                retry_max = max(retry_max, streak)
            else:
                streak = 0
            last_cmd = cmd
            last_failed = False
        elif ev.get("type") == "tool_result":
            last_failed = ev.get("status") == "error"

    failed_tool_calls = sum(
        1 for ev in events if ev.get("type") == "tool_result" and ev.get("status") == "error"
    )

    return {
        "planning": planning,
        "plan_items": plan_items,
        "plan_completed": plan_completed,
        "scope_adherence": scope_adherence,
        "self_test_after_last_edit": self_test,
        "self_test_kind": self_test_kind,
        "retry_pattern_max": retry_max,
        "failed_tool_calls": failed_tool_calls,
    }


def run_metrics(run_id: str) -> dict:
    events_path = RUNS_DIR / run_id / "events.jsonl"
    if not events_path.is_file():
        raise FileNotFoundError(f"找不到事件流: {events_path}（请先运行 collect.py）")
    events = load_events(events_path)
    signals = compute_signals(events)

    run_path = RESULTS_RUNS_DIR / f"{run_id}.run.json"
    run = load_json(run_path)
    run["process"] = signals
    dump_json(run, run_path)
    shutil.copy2(run_path, RUNS_DIR / run_id / "run.json")

    score_path = RESULTS_SCORES_DIR / f"{run_id}.score.json"
    if score_path.is_file():
        score = load_json(score_path)
        score["process_signals"] = signals
        dump_json(score, score_path)
    else:
        eprint(f"警告: {score_path} 不存在，仅更新 run.json（请先运行 verify.py）")
    return signals


def main() -> int:
    ap = argparse.ArgumentParser(description="L1 过程行为探测器")
    ap.add_argument("run_id")
    args = ap.parse_args()
    try:
        signals = run_metrics(args.run_id)
    except FileNotFoundError as exc:
        eprint(f"process_metrics 失败: {exc}")
        return 1
    for k, v in signals.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
