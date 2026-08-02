"""任务体检（advisory）：在跑任务前评估任务质量并给出建议，不设门槛。

用法：
  python scripts/check_task.py <task_id>
  python scripts/check_task.py <task_id> --json

特性：
  - 只读、永不阻断，始终 exit 0；输出仅为建议（OK / INFO / WARN）。
  - 默认人读文本；--json 输出机读结构，供文档/agent 消费。

检查项（均为建议级）：
  1. 结构：task.yaml / prompt.md / workspace/ / verifier/ 四件套是否齐全。
  2. 字段：task.yaml 必需键是否齐全、task_id 与目录名是否一致。
  3. verifier 可运行性：在 workspace/ 临时副本上按 copy_into_work 放入 verifier 并执行
     verifier.command，区分"用例正常失败（预期）"与"verifier 本身坏了（0 用例/收集错误）"。
  4. 部分分可用性：非 pytest 命令提示只会得到 0/1 全或无分。
  5. prompt 泄漏扫描：prompt.md 是否出现 verifier 文件名 / def test_* / verifier/ / assert 等
     疑似暴露检查手段的字样。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import TASKS_DIR, load_task

# 必需的 task.yaml 顶层键（verifier.command 单独检查）
REQUIRED_KEYS = ("task_id", "title", "category", "difficulty", "language",
                 "timeout_s", "prompt_version")

PYTEST_SUMMARY_RE = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "error": re.compile(r"(\d+)\s+error"),
}

# verifier 命令是否 pytest 风格（可给部分分）
PYTEST_CMD_RE = re.compile(r"\b(pytest|py\.test)\b", re.IGNORECASE)

# verifier 本身损坏的迹象（非"用例失败"，而是收集/导入层错误）
BROKEN_RE = re.compile(
    r"(ImportError|ModuleNotFoundError|no tests ran|errors during collection|"
    r"INTERNALERROR|collected 0 items)",
    re.IGNORECASE,
)

# prompt 泄漏扫描：疑似暴露 verifier 存在或检查手段的字样
LEAK_PATTERNS = {
    "测试函数名 def test_*": re.compile(r"\bdef\s+test_\w+", re.IGNORECASE),
    "verifier 目录": re.compile(r"\bverifier\b", re.IGNORECASE),
    "assert 断言": re.compile(r"\bassert\b"),
    "pytest 字样": re.compile(r"\bpytest\b", re.IGNORECASE),
}


class Findings:
    """收集建议级发现，永不阻断。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, message: str) -> None:
        self.items.append({"level": level, "message": message})

    def ok(self, msg: str) -> None:
        self.add("OK", msg)

    def info(self, msg: str) -> None:
        self.add("INFO", msg)

    def warn(self, msg: str) -> None:
        self.add("WARN", msg)

    def counts(self) -> dict:
        c = {"OK": 0, "INFO": 0, "WARN": 0}
        for it in self.items:
            c[it["level"]] = c.get(it["level"], 0) + 1
        return c


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


def check_structure(task_dir: Path, f: Findings, judge_only: bool = False) -> dict:
    """检查四件套是否齐全，返回各部分存在情况。"""
    parts = {
        "task.yaml": task_dir / "task.yaml",
        "prompt.md": task_dir / "prompt.md",
        "workspace/": task_dir / "workspace",
        "verifier/": task_dir / "verifier",
    }
    present = {}
    for name, path in parts.items():
        exists = path.is_dir() if name.endswith("/") else path.is_file()
        present[name] = exists
        if exists:
            f.ok(f"存在 {name}")
        elif name == "verifier/" and judge_only:
            f.info("judge-only 任务：无需 verifier/（L0 无客观分，由 L2 judge 盲评）")
        else:
            f.warn(f"缺少 {name}（预期路径 {path}）")
    return present


def check_fields(task: dict, task_id: str, f: Findings) -> None:
    missing = [k for k in REQUIRED_KEYS if task.get(k) in (None, "")]
    if missing:
        f.warn(f"task.yaml 缺少必需键：{', '.join(missing)}")
    else:
        f.ok("task.yaml 必需键齐全")

    if task.get("task_id") != task_id:
        f.warn(f"task.yaml 的 task_id={task.get('task_id')!r} 与目录名 {task_id!r} 不一致")
    else:
        f.ok("task_id 与目录名一致")

    verifier = task.get("verifier") or {}
    if not verifier.get("command"):
        f.info("judge-only 任务：未配置 verifier.command，L0 客观分缺省，由 L2 judge 盲评评分")


def check_verifier(task: dict, task_dir: Path, f: Findings) -> None:
    verifier = task.get("verifier") or {}
    command = verifier.get("command")
    if not command:
        return  # 已在 check_fields 中告警
    workspace_src = task_dir / "workspace"
    verifier_src = task_dir / "verifier"
    if not workspace_src.is_dir():
        f.info("无 workspace/，跳过 verifier 可运行性检查")
        return

    if PYTEST_CMD_RE.search(command):
        f.ok("verifier 为 pytest 风格，可得到 passed/total 部分分")
    else:
        f.info("verifier 非 pytest 风格：只会按退出码得到 0/1 全或无分")

    with tempfile.TemporaryDirectory(prefix="checktask-") as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(workspace_src, work)
        if verifier.get("copy_into_work", True) and verifier_src.is_dir():
            shutil.copytree(verifier_src, work / "verifier")
        timeout = int(task.get("timeout_s", 600))
        try:
            proc = subprocess.run(
                command, shell=True, cwd=work, capture_output=True,
                text=True, timeout=timeout, encoding="utf-8", errors="replace",
            )
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            f.warn(f"verifier 命令在 workspace 上超时（{timeout}s），请确认命令是否会挂起")
            return
        except OSError as exc:
            f.warn(f"verifier 命令无法执行：{exc}（确认解释器/依赖已安装）")
            return

    if BROKEN_RE.search(output):
        f.warn("verifier 疑似损坏：出现收集/导入层错误（非用例失败）。"
               "请检查 conftest.py 路径、依赖与 import。")
        return

    parsed = parse_pytest_output(output)
    if parsed is not None:
        passed, total = parsed
        if passed == total:
            f.info(f"verifier 在起点 workspace 上已全绿（{passed}/{total}）——"
                   "若这是 bugfix 任务，说明起点未包含待修复缺陷，请确认任务意图")
        else:
            f.ok(f"verifier 接线正常：起点 workspace 上 {passed}/{total} 通过"
                 "（用例正常失败属预期，因 workspace 是待修复起点）")
    else:
        # 非 pytest：只能靠退出码粗判
        if exit_code == 0:
            f.info("verifier 在起点 workspace 上退出码 0——"
                   "若为 bugfix 任务请确认起点确实包含缺陷")
        else:
            f.ok(f"verifier 可执行（起点退出码 {exit_code}，失败属预期）")


def check_prompt_leak(task_dir: Path, f: Findings) -> None:
    prompt_path = task_dir / "prompt.md"
    if not prompt_path.is_file():
        return
    text = prompt_path.read_text(encoding="utf-8", errors="replace")

    # 收集 verifier 目录下的文件名，用于精确泄漏检测
    verifier_dir = task_dir / "verifier"
    leaked_names = []
    if verifier_dir.is_dir():
        for p in verifier_dir.rglob("*"):
            if p.is_file() and p.name.lower() not in ("conftest.py", "__init__.py"):
                if p.name in text:
                    leaked_names.append(p.name)
    if leaked_names:
        f.warn(f"prompt.md 直接出现 verifier 文件名：{', '.join(sorted(set(leaked_names)))}")

    hit = False
    for label, pattern in LEAK_PATTERNS.items():
        if pattern.search(text):
            f.warn(f"prompt.md 疑似暴露检查手段：命中「{label}」")
            hit = True
    if not hit and not leaked_names:
        f.ok("prompt.md 未发现明显 verifier 泄漏")


def check_task(task_id: str) -> Findings:
    f = Findings()
    task_dir = TASKS_DIR / task_id
    if not task_dir.is_dir():
        f.warn(f"任务目录不存在：{task_dir}")
        return f

    task = None
    if (task_dir / "task.yaml").is_file():
        try:
            task = load_task(task_id)
        except Exception as exc:  # noqa: BLE001 - 建议级，吞掉解析异常
            f.warn(f"task.yaml 解析失败：{exc}")
    judge_only = task is not None and not (task.get("verifier") or {}).get("command")

    present = check_structure(task_dir, f, judge_only=judge_only)
    if task is None:
        return f  # 无/不可解析 task.yaml，无法继续字段/verifier 检查

    check_fields(task, task_id, f)
    if present.get("verifier/") or not judge_only:
        check_verifier(task, task_dir, f)
    # judge-only 任务没有 verifier，prompt.md 提到 pytest/assert 是正当任务要求而非检查手段泄漏
    if present.get("prompt.md") and not judge_only:
        check_prompt_leak(task_dir, f)
    return f


def print_text(task_id: str, f: Findings) -> None:
    icons = {"OK": "[OK]  ", "INFO": "[INFO]", "WARN": "[WARN]"}
    print(f"任务体检：{task_id}（建议级，不阻断）\n")
    for it in f.items:
        print(f"{icons.get(it['level'], it['level'])} {it['message']}")
    c = f.counts()
    print(f"\n小结：OK={c['OK']}  INFO={c['INFO']}  WARN={c['WARN']}")
    if c["WARN"]:
        print("提示：以上为建议，不强制采纳；如认可可据此改进任务后再入库。")


def main() -> int:
    ap = argparse.ArgumentParser(description="任务体检（advisory，不阻断）")
    ap.add_argument("task_id", help="tasks/ 下的目录名")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    args = ap.parse_args()

    f = check_task(args.task_id)
    if args.json:
        print(json.dumps(
            {"task_id": args.task_id, "counts": f.counts(), "findings": f.items},
            ensure_ascii=False, indent=2,
        ))
    else:
        print_text(args.task_id, f)
    return 0  # 永不阻断


if __name__ == "__main__":
    sys.exit(main())
