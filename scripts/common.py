"""bench 公共工具：路径、JSON IO、sha256、任务加载、schema 校验。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import yaml
import jsonschema

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = ROOT / "tasks"
RUNS_DIR = ROOT / "runs"
RESULTS_RUNS_DIR = ROOT / "results" / "runs"
RESULTS_SCORES_DIR = ROOT / "results" / "scores"
SCHEMAS_DIR = ROOT / "schemas"
REPORTS_DIR = ROOT / "reports"
CONFIG_DIR = ROOT / "config"

RUN_ID_RE = re.compile(r"^(\d{8})-(.+)-(.+)-(.+)-(\d{2})$")

AGENTS = ("claude-code", "codex", "qoder-cli", "opencode", "pi", "qwen", "kimi")


def list_tasks() -> list[dict]:
    """扫描 tasks/，返回按 difficulty 分组用的任务元数据列表。"""
    tasks = []
    if not TASKS_DIR.is_dir():
        return tasks
    for child in sorted(TASKS_DIR.iterdir()):
        task_yaml = child / "task.yaml"
        if not task_yaml.is_file():
            continue
        with open(task_yaml, "r", encoding="utf-8") as f:
            task = yaml.safe_load(f) or {}
        tasks.append({
            "task_id": task.get("task_id", child.name),
            "title": task.get("title", child.name),
            "category": task.get("category"),
            "difficulty": task.get("difficulty", "easy"),
            "language": task.get("language"),
            "timeout_s": task.get("timeout_s"),
        })
    return tasks


def load_json(path: Path | str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(data, path: Path | str, indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_schema(name: str) -> dict:
    return load_json(SCHEMAS_DIR / f"{name}.schema.json")


def validate(data: dict, schema_name: str) -> list[str]:
    """返回错误信息列表，空列表表示通过。"""
    schema = load_schema(schema_name)
    validator = jsonschema.Draft7Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{loc}: {err.message}")
    return errors


def load_task(task_id: str) -> dict:
    """加载 tasks/<task_id>/task.yaml，返回 dict。"""
    task_dir = TASKS_DIR / task_id
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        raise FileNotFoundError(f"任务不存在: {task_yaml}")
    with open(task_yaml, "r", encoding="utf-8") as f:
        task = yaml.safe_load(f)
    task["_dir"] = task_dir
    return task


def parse_run_id(run_id: str) -> dict | None:
    """解析 <yyyymmdd>-<task>-<agent>-<model>-<seq>；agent 段已知，便于切分。"""
    m = RUN_ID_RE.match(run_id)
    if not m:
        return None
    date, task, agent, model, seq = m.groups()
    return {"date": date, "task": task, "agent": agent, "model": model, "seq": seq}


def eprint(*args) -> None:
    print(*args, file=sys.stderr)
