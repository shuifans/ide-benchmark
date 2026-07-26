"""回收一次运行的日志并归一化成 run.json。

用法：
  python scripts/collect.py <run_id> [--transcript PATH] [--status completed]
      [--human-intervention N] [--credits X]

步骤：定位原生日志 → 解析归一化事件流（events.jsonl）→ 汇总 token/耗时 →
价格表折算成本 → 回读实际模型与 manifest 比对 → 写 results/runs/<run_id>.run.json。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from common import (
    RESULTS_RUNS_DIR, RUNS_DIR, dump_json, load_json, sha256_file, validate, eprint,
)
import pricing

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"
sys.path.insert(0, str(ADAPTERS_DIR))

from base import aggregate_stats, write_events, write_transcript_summary  # noqa: E402


def get_adapter(agent: str):
    if agent == "claude-code":
        from claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter()
    if agent == "qoder-cli":
        from qoder_cli import QoderCliAdapter
        return QoderCliAdapter()
    if agent == "opencode":
        from opencode import OpencodeAdapter
        return OpencodeAdapter()
    if agent == "codex":
        from codex import CodexAdapter
        return CodexAdapter()
    if agent == "pi":
        from pi import PiAdapter
        return PiAdapter()
    if agent == "qwen":
        from qwen import QwenAdapter
        return QwenAdapter()
    if agent == "kimi":
        from kimi import KimiAdapter
        return KimiAdapter()
    raise ValueError(f"未知 agent: {agent}")


def detect_harness_version(agent: str) -> str | None:
    cmd = {"claude-code": ["claude", "--version"], "qoder-cli": ["qodercli", "--version"],
           "opencode": ["opencode", "--version"], "codex": ["codex", "--version"],
           "pi": ["pi", "--version"], "qwen": ["qwen", "--version"],
           "kimi": ["kimi", "--version"]}.get(agent)
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        text = (out.stdout or out.stderr).strip().splitlines()[0]
        return text.split()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def models_match(declared: str, actual: str | None, harness: str | None = None) -> tuple[bool, str]:
    if not actual:
        return False, "日志中未找到模型信息，跳过比对"
    if resolve_eq(declared, actual):
        return False, f"模型一致: {actual}"
    if harness == "qoder-cli":
        from qoder_cli import MODEL_KEY_FAMILY
        family = MODEL_KEY_FAMILY.get(actual.strip().lower())
        if family and family in declared.strip().lower():
            return False, f"模型一致（qoder 别名 {actual} ↔ {declared}，档位以 manifest 声明为准）"
    return True, f"模型不一致: manifest 声明 {declared}，日志实际 {actual}"


def resolve_eq(a: str, b: str) -> bool:
    ra, rb = pricing.resolve_model(a), pricing.resolve_model(b)
    if ra and rb:
        return ra == rb
    return a.strip().lower() == b.strip().lower()


def collect(run_id: str, transcript: str | None, status: str,
            human_intervention: int, credits: float | None) -> Path:
    run_dir = RUNS_DIR / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"找不到 manifest: {manifest_path}")
    manifest = load_json(manifest_path)

    adapter = get_adapter(manifest["agent"])
    transcript_path = adapter.locate_session(run_dir, manifest, transcript)
    events = adapter.parse_events(transcript_path)
    if not events:
        raise RuntimeError(f"未能从日志解析出任何事件: {transcript_path}")

    events_path = run_dir / "events.jsonl"
    write_events(events, events_path)
    write_transcript_summary(events, run_dir / "transcript_summary.txt")

    stats = aggregate_stats(events)
    if stats["input_tokens"] == 0 and stats["output_tokens"] == 0:
        raise RuntimeError("token 统计全为 0，日志可能不含 usage 数据，该 run 作废")

    # 成本折算：优先 manifest 声明模型，未知则尝试实际模型
    cost_model = manifest["model"]
    cost = pricing.cost_usd(
        cost_model, stats["input_tokens"], stats["cache_read_tokens"],
        stats["output_tokens"], stats["cache_creation_tokens"],
    )
    if cost is None and stats["actual_model"]:
        cost_model = stats["actual_model"]
        cost = pricing.cost_usd(
            cost_model, stats["input_tokens"], stats["cache_read_tokens"],
            stats["output_tokens"], stats["cache_creation_tokens"],
        )
    if cost is None:
        raise RuntimeError(
            f"模型 {manifest['model']} / {stats['actual_model']} 不在价格表中，"
            f"请先在 scripts/pricing.py 补充价格"
        )

    mismatch, match_note = models_match(manifest["model"], stats["actual_model"], manifest["agent"])
    if stats["actual_model"] and stats["actual_model"] != cost_model:
        # 实际模型与计费模型不一致时按实际模型重新折算
        recost = pricing.cost_usd(
            stats["actual_model"], stats["input_tokens"], stats["cache_read_tokens"],
            stats["output_tokens"], stats["cache_creation_tokens"],
        )
        if recost is not None:
            cost, cost_model = recost, stats["actual_model"]

    notes = [match_note]
    if manifest["agent"] == "qoder-cli":
        from qoder_cli import aux_summary
        note = aux_summary(load_json(transcript_path))
        if note:
            notes.append(note)

    run = {
        "run_id": run_id,
        "task_id": manifest["task_id"],
        "agent": {
            "harness": manifest["agent"],
            "version": detect_harness_version(manifest["agent"]),
            "model": manifest["model"],
            "params": manifest.get("params") or {},
        },
        "model_mismatch": mismatch,
        "actual_model": stats["actual_model"],
        "human_intervention": human_intervention,
        "started_at": stats["started_at"],
        "ended_at": stats["ended_at"],
        "duration_s": stats["duration_s"],
        "usage": {
            "input_tokens": stats["input_tokens"],
            "cache_read_tokens": stats["cache_read_tokens"],
            "cache_creation_tokens": stats["cache_creation_tokens"],
            "output_tokens": stats["output_tokens"],
            "cost_usd": cost,
            "cost_source": "price_table",
            "price_version": pricing.PRICE_VERSION,
            "cost_model": cost_model,
            "credits_consumed": credits,
        },
        "tool_calls": stats["tool_calls"],
        "turns": stats["turns"],
        "status": status,
        "prompt_sha256": manifest["prompt_sha256"],
        "manifest_sha256": sha256_file(manifest_path),
        "notes": "；".join(notes),
    }

    errors = validate(run, "run")
    if errors:
        raise RuntimeError("run.json 未通过 schema 校验: " + "; ".join(errors))

    out = RESULTS_RUNS_DIR / f"{run_id}.run.json"
    dump_json(run, out)
    shutil.copy2(out, run_dir / "run.json")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回收运行日志并归一化 run.json")
    ap.add_argument("run_id", help="runs/ 下的目录名")
    ap.add_argument("--transcript", help="手动指定日志文件（绕过自动定位）")
    ap.add_argument("--status", choices=["completed", "timeout", "error"], default="completed")
    ap.add_argument("--human-intervention", type=int, default=0, help="运行中人工介入次数")
    ap.add_argument("--credits", type=float, default=None, help="Qoder Credits 实际消耗（仅附注）")
    args = ap.parse_args()

    try:
        out = collect(args.run_id, args.transcript, args.status,
                      args.human_intervention, args.credits)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        eprint(f"collect 失败: {exc}")
        return 1
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
