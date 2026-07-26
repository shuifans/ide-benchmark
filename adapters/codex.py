"""codex (OpenAI Codex CLI) adapter：解析 ~/.codex/sessions 下的 rollout JSONL。

日志位置：~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl
行格式：{"timestamp", "type": "session_meta"|"turn_context"|"response_item"|"event_msg", "payload": {...}}
- session_meta.payload.cwd 用于归属校验
- turn_context.payload.model 记录当前模型
- response_item.payload.type: message / function_call / function_call_output /
  local_shell_call / reasoning
- event_msg.payload.type == "token_count"：payload.info.last_token_usage
  {input_tokens(含 cached), cached_input_tokens, output_tokens, reasoning_output_tokens}
  归一化：input = input - cached；output 已含 reasoning。

注意：本适配器按已知上游格式编写，本机 codex 未安装未经实测；
首次真实运行后如解析异常，请用样例日志校准。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from base import Adapter, classify_tool, parse_ts

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def _session_cwd(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(5):
                line = f.readline().strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("type") == "session_meta":
                    return (rec.get("payload") or {}).get("cwd")
    except (OSError, json.JSONDecodeError):
        pass
    return None


class CodexAdapter(Adapter):
    name = "codex"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.jsonl"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        work_dir = str((run_dir / "work").resolve())
        if not CODEX_SESSIONS_DIR.is_dir():
            raise FileNotFoundError(
                f"找不到 codex 会话目录: {CODEX_SESSIONS_DIR}\n"
                f"请确认 codex 已安装且在 {work_dir} 下运行过；或用 --transcript 手动指定"
            )
        created = parse_ts(manifest.get("created_at") or "")
        candidates = []
        for p in CODEX_SESSIONS_DIR.glob("*/*/*/rollout-*.jsonl"):
            if created is not None and p.stat().st_mtime < created.timestamp() - 60:
                continue
            if _session_cwd(p) == work_dir:
                candidates.append(p)
        if not candidates:
            raise FileNotFoundError(
                f"在 {CODEX_SESSIONS_DIR} 下找不到 cwd={work_dir} 的 rollout 日志；"
                f"或用 --transcript 手动指定"
            )
        candidates.sort(key=lambda p: p.stat().st_mtime)
        shutil.copy2(candidates[-1], dest)
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
                ts = rec.get("timestamp")
                rtype = rec.get("type")
                payload = rec.get("payload") or {}

                if rtype == "turn_context":
                    current_model = payload.get("model") or current_model

                elif rtype == "response_item":
                    ptype = payload.get("type")
                    if ptype == "message":
                        role = payload.get("role")
                        text = self._content_text(payload.get("content"))
                        if text and role in ("user", "assistant"):
                            events.append({
                                "ts": ts,
                                "type": "user_message" if role == "user" else "assistant_message",
                                "text": text,
                            })
                    elif ptype == "function_call":
                        events.append(self._function_call_event(ts, payload))
                    elif ptype == "local_shell_call":
                        action = payload.get("action") or {}
                        cmd = action.get("command")
                        if isinstance(cmd, list):
                            cmd = " ".join(str(c) for c in cmd)
                        events.append({
                            "ts": ts, "type": "tool_call",
                            "name": "local_shell", "tool_class": "command",
                            "command": str(cmd or ""),
                        })
                    elif ptype == "function_call_output":
                        output = payload.get("output")
                        text, status = "", "ok"
                        if isinstance(output, dict):
                            text = str(output.get("content") or "")
                            meta = output.get("metadata") or {}
                            if meta.get("exit_code") not in (0, None):
                                status = "error"
                        else:
                            text = str(output or "")
                        events.append({"ts": ts, "type": "tool_result",
                                       "status": status, "text": text[:2000]})

                elif rtype == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or payload
                    last = info.get("last_token_usage") or {}
                    if last:
                        cached = int(last.get("cached_input_tokens") or 0)
                        usage = {
                            "input_tokens": max(int(last.get("input_tokens") or 0) - cached, 0),
                            "output_tokens": int(last.get("output_tokens") or 0),
                            "cache_read_tokens": cached,
                            "cache_creation_tokens": 0,
                        }
                        if any(usage.values()):
                            events.append({"ts": ts, "type": "assistant_message",
                                           "usage": usage, "model": current_model})
        return events

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")
            ).strip()
        return ""

    @staticmethod
    def _function_call_event(ts, payload: dict) -> dict:
        name = payload.get("name") or ""
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        ev = {
            "ts": ts,
            "type": "tool_call",
            "name": name,
            "tool_class": classify_tool(name),
        }
        if isinstance(args, dict):
            command = args.get("command") or args.get("cmd")
            if isinstance(command, list):
                command = " ".join(str(c) for c in command)
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
