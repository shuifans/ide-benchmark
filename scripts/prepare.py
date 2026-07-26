"""初始化一个测试目录（run）。

用法：
  python scripts/prepare.py --task py-fix-off-by-one --agent qoder-cli \
      --model deepseek-v4-pro --context-window 1M --thinking max

产出：
  runs/<run_id>/manifest.json   运行清单（锁定任务/prompt/模型/参数）
  runs/<run_id>/PROMPT.md       待粘贴给被测 agent 的提示词
  runs/<run_id>/work/           任务工作区（用被测 agent 打开此目录跑任务）
  stdout 打印 JSON 启动指引
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import AGENTS, RUNS_DIR, dump_json, load_task, sha256_file, validate, eprint

MANIFEST_VERSION = "1"

LAUNCH_GUIDE = {
    "claude-code": {
        "command": "claude --dangerously-skip-permissions",
        "note": "bypass 模式启动，无需逐次批准工具调用；"
                "若接入第三方模型（如 deepseek 经代理），请确认 transcript 仍记录 token usage",
    },
    "qoder-cli": {
        "command": "claude-tap --tap-client qoder --tap-no-open --dangerously-skip-permissions",
        "note": "必须经 claude-tap 包裹启动（否则采不到 token，该 run 作废）；"
                "--dangerously-skip-permissions 会透传给 qodercli 以 bypass 模式运行",
    },
    "opencode": {
        "command": "opencode --auto",
        "note": "--auto 自动批准权限；opencode 按启动目录归属 session，务必先 cd 进 work 目录；"
                "启动后选定模型再粘贴提示词",
    },
    "codex": {
        "command": "codex --dangerously-bypass-approvals-and-sandbox",
        "note": "bypass 模式启动（跳过审批与沙箱）；日志在 ~/.codex/sessions/ 下按日期归档",
    },
    "pi": {
        "command": "pi",
        "note": "pi 无权限门槛，工具直接执行；启动后用 /model 选模型、确认思考强度后粘贴提示词",
    },
    "qwen": {
        "command": "qwen --yolo",
        "note": "--yolo 自动批准全部操作；启动后确认模型后粘贴提示词",
    },
    "kimi": {
        "command": "kimi --yolo",
        "note": "--yolo 自动批准常规工具调用；启动后确认模型后粘贴提示词",
    },
}


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9.\-]+", "-", text.lower()).strip("-")
    return slug or "unknown"


def parse_context_window(value: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", value.strip())
    if not m:
        raise ValueError(f"无法解析上下文窗口: {value!r}（示例：200k / 1M / 1000000）")
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "k":
        num *= 1_000
    elif unit == "m":
        num *= 1_000_000
    return int(num)


def next_seq(date: str, task_id: str, agent: str, model_slug: str) -> int:
    prefix = f"{date}-{task_id}-{agent}-{model_slug}-"
    seq = 0
    if RUNS_DIR.exists():
        for child in RUNS_DIR.iterdir():
            if child.is_dir() and child.name.startswith(prefix):
                try:
                    seq = max(seq, int(child.name.rsplit("-", 1)[1]))
                except ValueError:
                    continue
    return seq + 1


def prepare(task_id: str, agent: str, model: str, context_window: str, thinking: str) -> dict:
    task = load_task(task_id)
    task_dir = Path(task["_dir"])
    workspace_src = task_dir / "workspace"
    prompt_src = task_dir / "prompt.md"
    if not workspace_src.is_dir():
        raise FileNotFoundError(f"任务缺少 workspace/: {workspace_src}")
    if not prompt_src.is_file():
        raise FileNotFoundError(f"任务缺少 prompt.md: {prompt_src}")

    cw = parse_context_window(context_window)
    date = datetime.now().strftime("%Y%m%d")
    model_slug = slugify(model)
    seq = next_seq(date, task_id, agent, model_slug)
    run_id = f"{date}-{task_id}-{agent}-{model_slug}-{seq:02d}"
    run_dir = RUNS_DIR / run_id
    work_dir = run_dir / "work"
    if run_dir.exists():
        raise FileExistsError(f"run 目录已存在: {run_dir}")

    run_dir.mkdir(parents=True)
    shutil.copytree(workspace_src, work_dir)
    shutil.copy2(prompt_src, run_dir / "PROMPT.md")

    prompt_file = run_dir / "PROMPT.md"
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "prompt_version": str(task.get("prompt_version", "v1")),
        "prompt_sha256": sha256_file(prompt_file),
        "prompt_file": "PROMPT.md",
        "agent": agent,
        "model": model,
        "params": {
            "context_window": cw,
            "thinking_effort": thinking,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    errors = validate(manifest, "manifest")
    if errors:
        raise ValueError("manifest 未通过 schema 校验: " + "; ".join(errors))
    dump_json(manifest, run_dir / "manifest.json")

    guide = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "work_dir": str(work_dir),
        "prompt_file": str(prompt_file),
        "agent": agent,
        "model": model,
        "params": manifest["params"],
        "launch": LAUNCH_GUIDE[agent],
        "timeout_s": task.get("timeout_s"),
    }
    return guide


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化 bench 测试目录")
    ap.add_argument("--task", required=True, help="任务 id（tasks/ 下的目录名）")
    ap.add_argument("--agent", required=True, choices=AGENTS)
    ap.add_argument("--model", required=True, help="模型名，如 deepseek-v4-pro")
    ap.add_argument("--context-window", required=True, help="上下文窗口，如 200k / 1M")
    ap.add_argument("--thinking", required=True, help="思考强度，如 max/high/medium/low")
    args = ap.parse_args()

    try:
        guide = prepare(args.task, args.agent, args.model, args.context_window, args.thinking)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        eprint(f"prepare 失败: {exc}")
        return 1

    print(json.dumps(guide, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
