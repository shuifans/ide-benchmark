"""kimi (Kimi Code CLI) adapter：解析 ~/.kimi-code/sessions 下的 wire.jsonl。

定位：~/.kimi-code/session_index.jsonl 提供 sessionId → sessionDir → workDir 映射；
主事件流在 <sessionDir>/agents/main/wire.jsonl（protocol_version 1.4）。

已用真实 probe 会话校准（2026-07-26，kimi 0.29.1）的事件结构：
- config.update / llm.request：modelAlias（如 kimi-code/kimi-for-coding）
- turn.prompt：{input: [{type: text, text}]} 用户输入
- context.append_loop_event.event：
    step.begin / content.part（part.type: think|text|tool_call…）/
    step.end（usage: {inputOther, output, inputCacheRead, inputCacheCreation}，每次 LLM 请求一条）
- usage.record：turn 级汇总（与 step.end 重复，不采）
- context.append_message：role user/tool 的上下文追加（含 system-reminder，均不采文本，
  仅取 role tool 的结果状态）

注意：probe 会话未包含工具调用，tool_call/tool_result 的确切字段按通用形状解析，
首次真实任务 run 后如发现遗漏请对照 wire.jsonl 校准。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from base import Adapter, classify_tool, parse_ts

KIMI_DIR = Path.home() / ".kimi-code"


def _index_entries() -> list[dict]:
    index = KIMI_DIR / "session_index.jsonl"
    entries = []
    if index.is_file():
        with open(index, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


class KimiAdapter(Adapter):
    name = "kimi"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.jsonl"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        work_dir = str((run_dir / "work").resolve())
        candidates: list[Path] = []
        for entry in _index_entries():
            entry_wd = entry.get("workDir") or ""
            # macOS 下 /tmp 会解析为 /private/tmp，做双向兼容
            if entry_wd != work_dir and str(Path(entry_wd).resolve()) != work_dir:
                continue
            wire = Path(entry.get("sessionDir", "")) / "agents" / "main" / "wire.jsonl"
            if wire.is_file():
                candidates.append(wire)
        if not candidates:
            raise FileNotFoundError(
                f"在 {KIMI_DIR}/session_index.jsonl 中找不到 workDir={work_dir} 的会话；"
                f"请确认是在该目录下运行的 kimi；或用 --transcript 手动指定 wire.jsonl"
            )
        created = parse_ts(manifest.get("created_at") or "")
        candidates.sort(key=lambda p: p.stat().st_mtime)
        chosen = candidates[-1]
        for p in reversed(candidates):
            if created is None or p.stat().st_mtime >= created.timestamp() - 60:
                chosen = p
                break
        shutil.copy2(chosen, dest)
        return dest

    def parse_events(self, transcript_path: Path) -> list[dict]:
        events: list[dict] = []
        current_model = None
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = str(rec.get("type") or "")
                ts = rec.get("time") or rec.get("timestamp")

                if rtype in ("config.update", "llm.request"):
                    current_model = rec.get("modelAlias") or rec.get("model") or current_model

                elif rtype == "turn.prompt":
                    text = self._content_text(rec.get("input"))
                    if text:
                        events.append({"ts": ts, "type": "user_message", "text": text})

                elif rtype == "context.append_loop_event":
                    ev = rec.get("event") or {}
                    etype = ev.get("type")
                    if etype == "content.part":
                        part = ev.get("part") or {}
                        ptype = part.get("type")
                        if ptype == "text" and part.get("text"):
                            events.append({"ts": ts, "type": "assistant_message",
                                           "text": part["text"]})
                        elif ptype in ("tool_call", "toolCall", "tool_use"):
                            events.append(self._tool_call_event(ts, part))
                        elif ptype in ("tool_result", "toolResult"):
                            events.append(self._tool_result_event(ts, part))
                    elif etype == "step.end":
                        usage = self._usage(ev.get("usage"))
                        if usage:
                            events.append({"ts": ts, "type": "assistant_message",
                                           "usage": usage, "model": current_model})

                elif rtype == "context.append_message":
                    msg = rec.get("message") or {}
                    role = msg.get("role")
                    if role in ("tool", "toolResult", "tool_result"):
                        events.append(self._tool_result_event(ts, msg))
                    elif role == "assistant":
                        for tc in msg.get("toolCalls") or []:
                            if isinstance(tc, dict):
                                events.append(self._tool_call_event(ts, tc))
        return events

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
            ).strip()
        return ""

    @staticmethod
    def _usage(raw) -> dict | None:
        """step.end 的 usage：{inputOther, output, inputCacheRead, inputCacheCreation}。"""
        if not isinstance(raw, dict) or not raw:
            return None
        u = {
            "input_tokens": int(raw.get("inputOther") or raw.get("input") or 0),
            "output_tokens": int(raw.get("output") or 0),
            "cache_read_tokens": int(raw.get("inputCacheRead") or 0),
            "cache_creation_tokens": int(raw.get("inputCacheCreation") or 0),
        }
        return u if any(u.values()) else None

    @classmethod
    def _tool_result_event(cls, ts, obj: dict) -> dict:
        text = cls._content_text(obj.get("content")) or str(obj.get("result") or obj.get("output") or "")
        is_error = bool(obj.get("isError") or obj.get("is_error") or obj.get("error"))
        return {"ts": ts, "type": "tool_result",
                "status": "error" if is_error else "ok", "text": text[:2000]}

    @staticmethod
    def _tool_call_event(ts, item: dict) -> dict:
        name = item.get("name") or item.get("tool") or item.get("toolName") or ""
        args = (item.get("arguments") or item.get("input") or item.get("args")
                or item.get("params") or {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        ev = {
            "ts": ts,
            "type": "tool_call",
            "name": name,
            "tool_class": classify_tool(name),
        }
        if isinstance(args, dict):
            command = args.get("command") or args.get("cmd")
            if command:
                ev["command"] = str(command)
            files = []
            for key in ("file_path", "path", "notebook_path"):
                if args.get(key):
                    files.append(str(args[key]))
            if files:
                ev["files"] = files
            if ev["tool_class"] == "plan":
                ev["plan_input"] = args
        return ev
