"""Claude Code adapter：解析原生 transcript JSONL。

默认日志位置：~/.claude/projects/<work_dir 的 slug>/*.jsonl
slug 规则：work_dir 绝对路径中的非字母数字字符替换为 "-"。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from base import Adapter, classify_tool

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def work_dir_slug(work_dir: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(work_dir.resolve()))


def _content_items(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.jsonl"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        work_dir = run_dir / "work"
        slug = work_dir_slug(work_dir)
        project_dir = CLAUDE_PROJECTS_DIR / slug
        if not project_dir.is_dir():
            raise FileNotFoundError(
                f"找不到 Claude Code 会话目录: {project_dir}\n"
                f"请确认是在 {work_dir} 下运行的 claude；或用 --transcript 手动指定 transcript 文件"
            )
        created_at = manifest.get("created_at") or ""
        candidates = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError(f"{project_dir} 下没有 transcript JSONL")
        # 优先选 manifest 创建之后修改过的最新文件
        chosen = candidates[-1]
        for p in reversed(candidates):
            from base import parse_ts
            created = parse_ts(created_at)
            if created is None or p.stat().st_mtime >= created.timestamp() - 60:
                chosen = p
                break
        shutil.copy2(chosen, dest)
        return dest

    def parse_events(self, transcript_path: Path) -> list[dict]:
        records = []
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # Claude Code 会把同一条 assistant 消息拆成多行 JSONL（thinking/text/tool_use 各一行），
        # 每行都带 usage 快照且数值不同（前几行是缓存解析前的快照，input 虚高、cache_read=0）。
        # 权威 usage = 每个 message.id 最后一条非空 usage（含真实的 input 增量 + cache_read 拆分）。
        # 按行累加会把中间快照重复计入 input，故先按 id 去重取最后一条。
        usage_by_id: dict[str, dict] = {}
        for i, rec in enumerate(records):
            if rec.get("type") != "assistant":
                continue
            m = rec.get("message") or {}
            raw = m.get("usage") or {}
            u = {
                "input_tokens": raw.get("input_tokens") or 0,
                "output_tokens": raw.get("output_tokens") or 0,
                "cache_read_tokens": raw.get("cache_read_input_tokens") or 0,
                "cache_creation_tokens": raw.get("cache_creation_input_tokens") or 0,
            }
            # 真实 transcript 总有 message.id；缺失时退化为逐行唯一 key（各记一次）
            key = m.get("id") or f"__rec{i}"
            if any(u.values()):
                usage_by_id[key] = u  # 同 id 后写覆盖 → 最后一条非空 usage 胜出

        events: list[dict] = []
        usage_emitted: set = set()
        for i, rec in enumerate(records):
                msg = rec.get("message") or {}
                ts = rec.get("timestamp")
                rec_type = rec.get("type")

                if rec_type == "assistant":
                    mid = msg.get("id") or f"__rec{i}"
                    model = msg.get("model")
                    # 每个 message.id 的权威 usage 只在首次出现时挂载一次
                    usage = None
                    if mid not in usage_emitted and mid in usage_by_id:
                        usage = usage_by_id[mid]
                    emitted_usage = False

                    def _attach(ev):
                        nonlocal emitted_usage
                        if usage is not None and not emitted_usage:
                            ev["usage"] = usage
                            ev["model"] = model
                            emitted_usage = True
                            usage_emitted.add(mid)
                        return ev

                    for item in _content_items(msg):
                        itype = item.get("type")
                        if itype == "text" and item.get("text"):
                            events.append(_attach({
                                "ts": ts, "type": "assistant_message",
                                "text": item["text"],
                            }))
                        elif itype == "tool_use":
                            events.append(_attach(self._tool_call_event(ts, item)))
                    if usage is not None and not emitted_usage:
                        events.append(_attach({
                            "ts": ts, "type": "assistant_message",
                        }))

                elif rec_type == "user":
                    for item in _content_items(msg):
                        itype = item.get("type")
                        if itype == "tool_result":
                            is_error = bool(item.get("is_error"))
                            content = item.get("content")
                            text = ""
                            if isinstance(content, str):
                                text = content
                            elif isinstance(content, list):
                                text = " ".join(
                                    c.get("text", "") for c in content if isinstance(c, dict)
                                )
                            events.append({
                                "ts": ts, "type": "tool_result",
                                "status": "error" if is_error else "ok",
                                "text": text[:2000],
                            })
                        elif itype == "text" and item.get("text"):
                            events.append({
                                "ts": ts, "type": "user_message",
                                "text": item["text"],
                            })
        return events

    @staticmethod
    def _tool_call_event(ts, item: dict) -> dict:
        name = item.get("name") or ""
        tool_input = item.get("input") or {}
        ev = {
            "ts": ts,
            "type": "tool_call",
            "name": name,
            "tool_class": classify_tool(name),
        }
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or tool_input.get("cmd")
            if command:
                ev["command"] = str(command)
            files = []
            for key in ("file_path", "path", "notebook_path"):
                if tool_input.get(key):
                    files.append(str(tool_input[key]))
            if files:
                ev["files"] = files
            # 计划类工具保留原始输入摘要，供 process_metrics 统计条目
            if ev["tool_class"] == "plan":
                ev["plan_input"] = tool_input
        return ev
