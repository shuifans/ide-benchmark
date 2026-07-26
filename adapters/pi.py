"""pi (pi-coding-agent) adapter：解析 ~/.pi/agent/sessions 下的会话 JSONL。

日志位置：~/.pi/agent/sessions/<safePath>/<ISO时间戳>_<uuid>.jsonl
safePath 规则：`--` + cwd 去掉开头的 / 后把 [/\\:] 替换为 `-` + `--`。
首行 {"type":"session", "cwd": ...} 可校验归属。

usage 位于 assistant message 上：{input, output, cacheRead, cacheWrite, reasoning}；
reasoning token 按输出价计费，归一化时并入 output_tokens。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from base import Adapter, classify_tool, parse_ts

PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"


def cwd_safe_path(work_dir: Path) -> str:
    resolved = str(work_dir.resolve())
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", resolved)) + "--"


def _session_cwd(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        rec = json.loads(first)
        if rec.get("type") == "session":
            return rec.get("cwd")
    except (OSError, json.JSONDecodeError):
        pass
    return None


class PiAdapter(Adapter):
    name = "pi"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.jsonl"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        work_dir = (run_dir / "work").resolve()
        session_dir = PI_SESSIONS_DIR / cwd_safe_path(work_dir)
        candidates: list[Path] = []
        if session_dir.is_dir():
            candidates = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            # 兜底：全量扫描按首行 cwd 匹配
            for p in PI_SESSIONS_DIR.glob("*/*.jsonl"):
                if _session_cwd(p) == str(work_dir):
                    candidates.append(p)
            candidates.sort(key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError(
                f"找不到 pi 会话日志: {session_dir}\n"
                f"请确认是在 {work_dir} 下运行的 pi；或用 --transcript 手动指定"
            )
        created = parse_ts(manifest.get("created_at") or "")
        chosen = candidates[-1]
        for p in reversed(candidates):
            if created is None or p.stat().st_mtime >= created.timestamp() - 60:
                chosen = p
                break
        shutil.copy2(chosen, dest)
        return dest

    def parse_events(self, transcript_path: Path) -> list[dict]:
        events: list[dict] = []
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "message":
                    continue
                msg = rec.get("message") or {}
                ts = rec.get("timestamp")
                role = msg.get("role")
                content = msg.get("content")
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                content = content or []

                if role == "user":
                    text = " ".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ).strip()
                    if text:
                        events.append({"ts": ts, "type": "user_message", "text": text})

                elif role == "assistant":
                    usage = self._usage(msg.get("usage"))
                    model = msg.get("model")
                    emitted = False
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        itype = item.get("type")
                        ev = None
                        if itype == "text" and item.get("text"):
                            ev = {"ts": ts, "type": "assistant_message", "text": item["text"]}
                        elif itype == "toolCall":
                            ev = self._tool_call_event(ts, item)
                        if ev is not None:
                            if usage and not emitted:
                                ev["usage"] = usage
                                ev["model"] = model
                                emitted = True
                            events.append(ev)
                    if usage and not emitted:
                        events.append({"ts": ts, "type": "assistant_message",
                                       "usage": usage, "model": model})

                elif role == "toolResult":
                    text = " ".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                    events.append({
                        "ts": ts, "type": "tool_result",
                        "status": "error" if msg.get("isError") else "ok",
                        "text": text[:2000],
                    })
        return events

    @staticmethod
    def _usage(raw) -> dict | None:
        if not isinstance(raw, dict) or not raw:
            return None
        u = {
            "input_tokens": int(raw.get("input") or 0),
            # reasoning 按输出价计费，并入 output
            "output_tokens": int(raw.get("output") or 0) + int(raw.get("reasoning") or 0),
            "cache_read_tokens": int(raw.get("cacheRead") or 0),
            "cache_creation_tokens": int(raw.get("cacheWrite") or 0),
        }
        return u if any(u.values()) else None

    @staticmethod
    def _tool_call_event(ts, item: dict) -> dict:
        name = item.get("name") or ""
        args = item.get("arguments") or {}
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
