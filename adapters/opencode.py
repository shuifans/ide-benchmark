"""opencode adapter：从 opencode 的全局 SQLite 库采集 token 与过程事件。

实测库结构（opencode 1.18.3）：DB 位于 ~/.local/share/opencode/opencode.db
- session(id, directory, agent, model, cost, tokens_input, tokens_output,
          tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, ...)
  · directory 为启动 opencode 的工作目录（正斜杠路径），用于匹配本次 run 的 work 目录
  · model 为 JSON，如 {"id":"deepseek-v4-pro","providerID":"bailian","variant":"high"}
  · tokens_input 已是"非缓存输入"（Anthropic 式：total=input+output+reasoning+cache_read+cache_write）
- message(id, session_id, time_created, data) — data 含 role / tokens / modelID / time
  · assistant 消息 data.tokens = {input, output, reasoning, cache:{read, write}}，为每步增量
- part(id, message_id, session_id, time_created, data) — data.type ∈
  {text, reasoning, tool, step-start, step-finish, file, patch}
  · tool: {type:"tool", tool:"edit", state:{status, input:{filePath/command/...}, output, time}}

用户须在 work 目录内启动 opencode 跑任务，adapter 按 directory + 时间窗定位本次 session。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from base import Adapter, classify_tool

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _db_path() -> Path:
    return Path(os.environ.get("OPENCODE_DB", str(DEFAULT_DB)))


def _norm_dir(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _parse_model(model_field) -> str | None:
    """session.model 可能是 JSON 字符串或 dict，取其中的 id。"""
    if not model_field:
        return None
    if isinstance(model_field, str):
        try:
            model_field = json.loads(model_field)
        except ValueError:
            return model_field
    if isinstance(model_field, dict):
        return model_field.get("id") or model_field.get("modelID")
    return None


class OpencodeAdapter(Adapter):
    name = "opencode"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.opencode.json"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        db_path = _db_path()
        if not db_path.is_file():
            raise FileNotFoundError(
                f"找不到 opencode 数据库: {db_path}\n"
                f"请确认 opencode 已运行过；或用 --transcript 手动指定导出的 session JSON"
            )
        work_dir = _norm_dir(str((run_dir / "work").resolve()))
        dump = self._extract_session(db_path, work_dir, manifest.get("created_at"))
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        return dest

    def _created_ms(self, created_at: str | None) -> int:
        if not created_at:
            return 0
        from base import parse_ts
        dt = parse_ts(created_at)
        return int(dt.timestamp() * 1000) if dt else 0

    def _extract_session(self, db_path: Path, work_dir: str, created_at: str | None) -> dict:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            work_dir = _norm_dir(work_dir)
            floor_ms = self._created_ms(created_at)
            candidates = con.execute(
                "SELECT id, directory, agent, model, cost, tokens_input, tokens_output, "
                "tokens_reasoning, tokens_cache_read, tokens_cache_write, "
                "time_created, time_updated FROM session ORDER BY time_created"
            ).fetchall()
            # 按 work 目录匹配 + 创建时间不早于 manifest（留 60s 余量）
            matched = [
                s for s in candidates
                if _norm_dir(s["directory"] or "") == work_dir
                and (floor_ms == 0 or (s["time_created"] or 0) >= floor_ms - 60_000)
            ]
            if not matched:
                any_dir = [s["directory"] for s in candidates if s["directory"]]
                raise RuntimeError(
                    f"opencode 库中未找到 work 目录 {work_dir} 且时间匹配的 session；"
                    f"请确认是在 work 目录内启动的 opencode。近期目录样例: {any_dir[-3:]}"
                )
            s = matched[-1]
            sid = s["id"]

            msgs = con.execute(
                "SELECT id, time_created, data FROM message "
                "WHERE session_id=? ORDER BY time_created", (sid,)).fetchall()
            parts = con.execute(
                "SELECT message_id, time_created, data FROM part "
                "WHERE session_id=? ORDER BY time_created", (sid,)).fetchall()

            parts_by_msg: dict[str, list] = {}
            for p in parts:
                parts_by_msg.setdefault(p["message_id"], []).append(json.loads(p["data"]))

            messages = []
            for m in msgs:
                d = json.loads(m["data"])
                messages.append({
                    "id": m["id"],
                    "time_created": m["time_created"],
                    "role": d.get("role"),
                    "modelID": d.get("modelID"),
                    "tokens": d.get("tokens") or {},
                    "parts": parts_by_msg.get(m["id"], []),
                })

            return {
                "session_id": sid,
                "directory": s["directory"],
                "agent": s["agent"],
                "model": _parse_model(s["model"]),
                "opencode_cost": s["cost"],
                "session_tokens": {
                    "input": s["tokens_input"], "output": s["tokens_output"],
                    "reasoning": s["tokens_reasoning"],
                    "cache_read": s["tokens_cache_read"], "cache_write": s["tokens_cache_write"],
                },
                "time_created": s["time_created"],
                "time_updated": s["time_updated"],
                "messages": messages,
            }
        finally:
            con.close()

    def parse_events(self, transcript_path: Path) -> list[dict]:
        with open(transcript_path, "r", encoding="utf-8") as f:
            dump = json.load(f)
        events: list[dict] = []
        session_model = dump.get("model")

        for msg in dump.get("messages", []):
            ts = msg.get("time_created")
            role = msg.get("role")
            model = msg.get("modelID") or session_model
            oc_parts = msg.get("parts") or []

            if role == "user":
                text = " ".join(
                    p.get("text", "") for p in oc_parts if p.get("type") == "text")
                events.append({"ts": ts, "type": "user_message", "text": text[:4000]})
                continue

            if role != "assistant":
                continue

            # 文本 / 推理 → assistant_message（usage 只挂在本消息的首个事件上，避免重复计数）
            tokens = msg.get("tokens") or {}
            cache = tokens.get("cache") or {}
            usage = {
                "input_tokens": tokens.get("input", 0),  # opencode 已是非缓存输入
                # reasoning 按输出价计费，并入 output
                "output_tokens": tokens.get("output", 0) + tokens.get("reasoning", 0),
                "cache_read_tokens": cache.get("read", 0),
                "cache_creation_tokens": cache.get("write", 0),
            }
            usage_attached = False

            def take_usage():
                nonlocal usage_attached
                if usage_attached or not any(usage.values()):
                    return None, None
                usage_attached = True
                return usage, model

            text_bits = [p.get("text", "") for p in oc_parts
                         if p.get("type") in ("text", "reasoning") and p.get("text")]
            if text_bits:
                u, mdl = take_usage()
                ev = {"ts": ts, "type": "assistant_message", "text": " ".join(text_bits)[:4000]}
                if u:
                    ev["usage"], ev["model"] = u, mdl
                events.append(ev)

            for p in oc_parts:
                if p.get("type") != "tool":
                    continue
                u, mdl = take_usage()
                call_ev = self._tool_event(ts, p)
                if u:
                    call_ev["usage"], call_ev["model"] = u, mdl
                events.append(call_ev)
                state = p.get("state") or {}
                status = state.get("status")
                events.append({
                    "ts": ts, "type": "tool_result",
                    "status": "error" if status == "error" else "ok",
                    "text": str(state.get("output") or state.get("error") or "")[:2000],
                })

            # 消息无文本无工具但有 usage（纯 step-finish）时兜底挂一个事件
            if not usage_attached and any(usage.values()):
                events.append({"ts": ts, "type": "assistant_message",
                               "usage": usage, "model": model})
        return events

    @staticmethod
    def _tool_event(ts, part: dict) -> dict:
        name = part.get("tool") or ""
        state = part.get("state") or {}
        tool_input = state.get("input") or {}
        ev = {"ts": ts, "type": "tool_call", "name": name, "tool_class": classify_tool(name)}
        if isinstance(tool_input, dict):
            if tool_input.get("command"):
                ev["command"] = str(tool_input["command"])
            files = [str(tool_input[k]) for k in ("filePath", "file_path", "path") if tool_input.get(k)]
            if files:
                ev["files"] = files
            if ev["tool_class"] == "plan":
                ev["plan_input"] = tool_input
        return ev
