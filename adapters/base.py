"""adapter 基类：把各 harness 的原生日志归一化成统一的 events.jsonl。

事件格式（每行一个 JSON）：
  ts:         ISO 时间戳（可为 None）
  type:       user_message | assistant_message | tool_call | tool_result
  tool_class: edit | command | read | plan | other   （仅 tool_call）
  name:       工具名（仅 tool_call/tool_result）
  command:    shell 命令文本（command 类 tool_call）
  files:      涉及的文件路径（edit/read 类 tool_call）
  status:     ok | error（仅 tool_result）
  text:       消息文本（仅 message 类）
  usage:      {input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}
  model:      该次调用实际模型

下游（process_metrics / transcript_summary / collect 汇总）只消费 events.jsonl。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

EDIT_TOOLS = {
    "edit", "multiedit", "write", "notebookedit", "str_replace_editor",
    "str_replace", "write_file", "apply_patch", "create_file", "search_replace",
    "replace", "edit_file",
}
COMMAND_TOOLS = {
    "bash", "shell", "run_command", "execute_command", "terminal", "cmd",
    "run_terminal_command", "run_terminal_cmd", "run_shell_command", "local_shell",
}
READ_TOOLS = {
    "read", "glob", "grep", "ls", "view", "read_file", "search", "list_dir",
    "list_directory", "search_file_content", "read_many_files",
}
PLAN_TOOLS = {
    "todowrite", "todoread", "taskcreate", "taskupdate", "exitplanmode",
    "update_plan", "plan", "todo_write", "todo_read",
}

TEST_COMMAND_RE = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+test|"
    r"jest|vitest|mocha|cargo\s+test|go\s+test|mvn\s+test|gradle(w)?\s+test|"
    r"make\s+test|ctest|dotnet\s+test|rspec|phpunit|tsc\b|npm\s+run\s+build|pnpm\s+build)",
    re.IGNORECASE,
)

# 临时验证（非测试框架）：解释器 -c/-e 执行内联脚本（需配合 assert 提示），如 python -c / node -e
ADHOC_TEST_RE = re.compile(
    r"\b(?:python3?|node|deno|bun|ruby|php)\s+-[ce]\b"
    r"|\bpython3?\s+\S*(?:test|check|verify)\w*\.py",
    re.IGNORECASE,
)
ASSERT_HINT_RE = re.compile(r"\bassert\b")


def classify_tool(name: str) -> str:
    n = (name or "").strip().lower()
    if n in EDIT_TOOLS:
        return "edit"
    if n in COMMAND_TOOLS:
        return "command"
    if n in READ_TOOLS:
        return "read"
    if n in PLAN_TOOLS:
        return "plan"
    return "other"


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # 秒或毫秒时间戳
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts).astimezone()
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def open_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读语义打开 SQLite 库：普通连接 + PRAGMA query_only。

    不能用 file:...?mode=ro URI：WAL 模式的库在磁盘上没有 -shm/-wal 文件时，
    纯只读连接无法创建共享内存索引，connect 能成功但首条查询即报
    'unable to open database file'（SQLITE_CANTOPEN）。普通连接允许 SQLite
    自行管理 -shm/-wal，PRAGMA query_only=ON 保证本进程绝不写入数据。
    """
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA query_only = ON")
    return con


def write_events(events: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def load_events(events_path: Path) -> list[dict]:
    events = []
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def aggregate_stats(events: list[dict]) -> dict:
    """从归一化事件流汇总指标。"""
    input_tokens = output_tokens = cache_read = cache_creation = 0
    turns = tool_calls = failed_tool_calls = 0
    models: dict[str, int] = {}
    first_ts = last_ts = None

    for ev in events:
        ts = parse_ts(ev.get("ts"))
        if ts:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts

        usage = ev.get("usage") or {}
        if usage:
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_read += int(usage.get("cache_read_tokens") or 0)
            cache_creation += int(usage.get("cache_creation_tokens") or 0)
            if ev.get("type") == "assistant_message":
                turns += 1
            if ev.get("model"):
                models[ev["model"]] = models.get(ev["model"], 0) + 1

        if ev.get("type") == "tool_call":
            tool_calls += 1
        elif ev.get("type") == "tool_result" and ev.get("status") == "error":
            failed_tool_calls += 1

    duration_s = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0.0
    actual_model = max(models, key=models.get) if models else None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "turns": turns,
        "tool_calls": tool_calls,
        "failed_tool_calls": failed_tool_calls,
        "started_at": first_ts.isoformat() if first_ts else None,
        "ended_at": last_ts.isoformat() if last_ts else None,
        "duration_s": round(duration_s, 1),
        "actual_model": actual_model,
    }


def write_transcript_summary(events: list[dict], out_path: Path, max_chars: int = 12000) -> None:
    """生成供 judge 使用的 transcript 摘要：开头/结尾消息 + 工具调用序列。"""
    head_texts: list[str] = []
    tail_texts: list[str] = []
    tool_lines: list[str] = []

    messages = [ev for ev in events if ev.get("type") in ("user_message", "assistant_message") and ev.get("text")]
    for ev in messages[:6]:
        head_texts.append(f"[{ev['type']}] {ev['text'][:800]}")
    for ev in messages[-6:]:
        tail_texts.append(f"[{ev['type']}] {ev['text'][:800]}")

    for i, ev in enumerate(e for e in events if e.get("type") == "tool_call"):
        desc = ev.get("name") or "?"
        if ev.get("command"):
            desc += f": {ev['command'][:160]}"
        elif ev.get("files"):
            desc += f": {', '.join(ev['files'][:4])}"
        tool_lines.append(f"{i + 1}. {desc}")

    parts = [
        "# transcript 摘要",
        "",
        "## 开头消息",
        *head_texts,
        "",
        f"## 工具调用序列（共 {len(tool_lines)} 次）",
        *tool_lines,
        "",
        "## 结尾消息",
        *tail_texts,
    ]
    text = "\n".join(parts)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n\n...（中间截断）...\n\n" + text[-half:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)


class Adapter:
    """harness 适配器接口。"""

    name = "base"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        """定位原生日志并复制到 run_dir/transcript.*，返回复制后的路径。"""
        raise NotImplementedError

    def parse_events(self, transcript_path: Path) -> list[dict]:
        """把原生日志解析成归一化事件列表。"""
        raise NotImplementedError
