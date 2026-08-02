"""任务创建落盘：Web 向导「新建自定义任务」入口，产出 judge-only 任务。

judge-only 任务 = 三件套：task.yaml（无 verifier 块）+ prompt.md + workspace/.gitkeep。
L0 无客观分（verify.py 写 status="skipped" 占位段），综合分由 L2 judge 盲评（代码质量）
+ P 过程 + T/D 效率按可用权重和归一得出（见 report.py docstring 的 judge-only 段）。

通常由 web/server.py 的 /api/tasks 端点调用；也可单测直接调用 create_task()。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from common import TASKS_DIR

# task_id 会作为目录名并嵌入 run_id（schemas/manifest.schema.json 的 run_id 模式
# 约束各段为 [a-z0-9][a-z0-9.-]*），此处强制同一字符集，否则 prepare 阶段
# manifest schema 校验会以含糊的报错失败。
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
TASK_ID_MAX_LEN = 64
DIFFICULTIES = ("easy", "medium", "hard")

YAML_HEADER = (
    "# judge-only 任务：无 verifier，L0 客观分缺省，综合分由 L2 judge 盲评（代码质量）\n"
    "# + P 过程 + T/D 效率按可用权重和（默认 65）归一得出。\n"
    "# 由 Web 向导创建；如需客观验证，请手工补充 verifier/ 目录与 verifier.command。\n"
)


def create_task(task_id: str, title: str, category: str, difficulty: str,
                language: str, timeout_s, prompt_md: str) -> dict:
    """落盘 judge-only 任务：tasks/<id>/{task.yaml, prompt.md, workspace/.gitkeep}。

    校验失败抛 ValueError；task_id 已存在抛 FileExistsError。
    返回 {"task_id", "task_dir", "judge_only": True}。
    """
    task_id = (task_id or "").strip()
    title = (title or "").strip()
    prompt_md = (prompt_md or "").strip()
    category = (category or "").strip() or "feature"
    language = (language or "").strip() or "python"

    # 正则已涵盖对 "."/".." 与路径分隔符的拒绝（须以字母数字开头，字符集内无 "/")
    if not TASK_ID_RE.fullmatch(task_id) or len(task_id) > TASK_ID_MAX_LEN:
        raise ValueError(
            f"task_id 非法：{task_id!r}（仅限小写字母/数字/点/连字符，"
            f"须以字母或数字开头，≤ {TASK_ID_MAX_LEN} 字符）")
    if not title:
        raise ValueError("标题不能为空")
    if not prompt_md:
        raise ValueError("任务提示词不能为空")
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty 非法：{difficulty!r}（可选 {'/'.join(DIFFICULTIES)}）")
    try:
        timeout_s = int(timeout_s)
    except (TypeError, ValueError):
        raise ValueError(f"timeout_s 非法：{timeout_s!r}（需为正整数秒）") from None
    if timeout_s <= 0:
        raise ValueError(f"timeout_s 需为正整数秒：{timeout_s}")

    task_dir = TASKS_DIR / task_id
    try:
        # 原子防重：目录已存在即失败（兼防 ThreadingHTTPServer 双击竞态）
        task_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(f"任务已存在：{task_dir}") from None

    task_doc = {
        "task_id": task_id,
        "title": title,
        "category": category,
        "difficulty": difficulty,
        "language": language,
        "timeout_s": timeout_s,
        "prompt_version": "v1",
    }
    try:
        with open(task_dir / "task.yaml", "w", encoding="utf-8") as f:
            f.write(YAML_HEADER)
            yaml.safe_dump(task_doc, f, sort_keys=False, allow_unicode=True,
                           default_flow_style=False)
        with open(task_dir / "prompt.md", "w", encoding="utf-8") as f:
            f.write(f"<!-- prompt_version: v1 -->\n{prompt_md}\n")
        (task_dir / "workspace").mkdir()
        (task_dir / "workspace" / ".gitkeep").touch()
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)  # 半成品回滚
        raise

    return {"task_id": task_id, "task_dir": str(task_dir), "judge_only": True}
