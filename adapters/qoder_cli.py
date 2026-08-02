"""Qoder CLI adapter：从 claude-tap 的 trace SQLite 库采集 token 与过程事件。

实测库结构（claude-tap 0.1.136 + qodercli 1.0.48）：
- sessions(id, started_at, updated_at, client, ...) — client='qoder'
- records(session_id, record_index, turn, timestamp, payload_json)
  payload_json = {timestamp, duration_ms, request:{method,path,headers,body},
                  response:{status,headers,body}}
- LLM 调用识别：request.path 含 "agent_chat_generation"；响应体为 SSE 流，
  每个 data 行是二次编码 JSON（外层 body 字段是内层 JSON 字符串），
  OpenAI 风格 chunk：choices[].delta.{content,reasoning_content,tool_calls} + usage
- 请求体加密不可读；模型以请求头 X-Model-Key 为准（dmodel=DeepSeek 系列等别名）
- 辅助调用（如标题生成，X-Model-Key=lite）不计入 token 汇总，单独记录

qodercli 必须经 `claude-tap --tap-client qoder` 启动，否则库中无本次数据。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from base import Adapter, classify_tool

DEFAULT_DB = Path.home() / ".local" / "share" / "claude-tap" / "traces.sqlite3"

LLM_PATH_MARK = "agent_chat_generation"

# qoder X-Model-Key 别名 → 模型家族（用于与 manifest 声明模型做一致性比对）
MODEL_KEY_FAMILY = {
    "dmodel": "deepseek",       # DeepSeek V4 Pro（推理增强）
    "dfmodel": "deepseek",      # DeepSeek V4 Flash（快速）
    "mmodel": "auto",           # 混合/自动路由
    "lite": "lite",             # 辅助调用（标题生成等）
}


def _db_path() -> Path:
    return Path(os.environ.get("CLAUDE_TAP_DB", str(DEFAULT_DB)))


def _parse_sse(body: str) -> dict:
    """解析 SSE 流，返回 {text, reasoning, tools, usage}。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_slots: dict[int, dict] = {}
    usage = None

    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            outer = json.loads(line[5:])
        except ValueError:
            continue
        inner = outer.get("body")
        if not isinstance(inner, str) or inner == "[DONE]":
            continue
        try:
            chunk = json.loads(inner)
        except ValueError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                slot = tool_slots.setdefault(tc.get("index", 0), {"name": None, "arguments": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tools = []
    for idx in sorted(tool_slots):
        slot = tool_slots[idx]
        args = slot["arguments"]
        tools.append({"name": slot["name"], "arguments": args})
    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tools": tools,
        "usage": usage or {},
    }


class QoderCliAdapter(Adapter):
    name = "qoder-cli"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.tap.json"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        db_path = _db_path()
        if not db_path.is_file():
            raise FileNotFoundError(
                f"找不到 claude-tap trace 库: {db_path}\n"
                f"请确认 qodercli 是经 `claude-tap --tap-client qoder` 启动的"
            )
        dump = self._extract_session(db_path, manifest.get("created_at"))
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        return dest

    def _extract_session(self, db_path: Path, created_at: str | None) -> dict:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"sessions", "records"} <= tables:
                raise RuntimeError(f"trace 库结构不符合预期（需要 sessions/records 表），实际: {sorted(tables)}")

            sessions = con.execute(
                "SELECT id, started_at, updated_at FROM sessions "
                "WHERE client='qoder' ORDER BY started_at"
            ).fetchall()
            if not sessions:
                raise RuntimeError("trace 库中没有 client='qoder' 的 session，"
                                   "qodercli 可能未经 claude-tap 启动")

            chosen = sessions[-1]
            if created_at:
                later = [s for s in sessions if str(s[1]) >= created_at]
                if later:
                    chosen = later[0]
            session_id, started_at, updated_at = chosen

            rows = con.execute(
                "SELECT record_index, timestamp, payload_json FROM records "
                "WHERE session_id=? ORDER BY record_index", (session_id,)).fetchall()

            requests: list[dict] = []
            aux: list[dict] = []
            for idx, ts, payload in rows:
                try:
                    d = json.loads(payload)
                except ValueError:
                    continue
                req = d.get("request") or {}
                resp = d.get("response") or {}
                if LLM_PATH_MARK not in str(req.get("path") or ""):
                    continue
                body = resp.get("body")
                if not isinstance(body, str):
                    continue
                parsed = _parse_sse(body)
                headers = req.get("headers") or {}
                model_key = next((v for k, v in headers.items()
                                  if k.lower() == "x-model-key"), None)
                usage = parsed["usage"]
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                rec = {
                    "record_index": idx,
                    "ts": d.get("timestamp") or ts,
                    "duration_ms": d.get("duration_ms"),
                    "model_key": model_key,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "cache_read_tokens": cached,
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
                    "text": parsed["text"],
                    "reasoning": parsed["reasoning"],
                    "tools": parsed["tools"],
                }
                requests.append(rec)

            if not requests:
                raise RuntimeError(
                    f"session {session_id} 中没有 LLM 调用记录（{LLM_PATH_MARK}），"
                    f"任务可能未真正执行")

            # 主模型 = 出现次数最多的 model_key；其余为辅助调用（标题生成等）
            from collections import Counter
            key_counts = Counter(r["model_key"] for r in requests)
            main_key = key_counts.most_common(1)[0][0]
            main, aux = [], []
            for r in requests:
                (main if r["model_key"] == main_key else aux).append(r)

            return {
                "session_id": str(session_id),
                "started_at": started_at,
                "updated_at": updated_at,
                "main_model_key": main_key,
                "requests": main,
                "aux_requests": aux,
            }
        finally:
            con.close()

    def parse_events(self, transcript_path: Path) -> list[dict]:
        with open(transcript_path, "r", encoding="utf-8") as f:
            dump = json.load(f)
        events: list[dict] = []
        main_key = dump.get("main_model_key")

        for req in dump.get("requests", []):
            ts = req.get("ts")
            usage = {
                "input_tokens": max(req.get("prompt_tokens", 0) - req.get("cache_read_tokens", 0), 0),
                "output_tokens": req.get("completion_tokens", 0),
                "cache_read_tokens": req.get("cache_read_tokens", 0),
                "cache_creation_tokens": 0,
            }
            # 文本与首个事件携带 usage（聚合按事件去重：仅挂在一个事件上）
            text = (req.get("text") or "").strip()
            reasoning = (req.get("reasoning") or "").strip()
            body_text = text or (f"(thinking) {reasoning[:400]}" if reasoning else "")
            events.append({
                "ts": ts, "type": "assistant_message",
                "text": body_text, "usage": usage, "model": req.get("model_key") or main_key,
            })
            for tool in req.get("tools", []):
                events.append(self._tool_event(ts, tool))
        return events

    @staticmethod
    def _tool_event(ts, tool: dict) -> dict:
        name = tool.get("name") or ""
        ev = {"ts": ts, "type": "tool_call", "name": name, "tool_class": classify_tool(name)}
        try:
            args = json.loads(tool.get("arguments") or "{}")
        except ValueError:
            args = {}
        if isinstance(args, dict):
            if args.get("command"):
                ev["command"] = str(args["command"])
            files = [str(args[k]) for k in ("file_path", "path", "notebook_path") if args.get(k)]
            if files:
                ev["files"] = files
            if ev["tool_class"] == "plan":
                ev["plan_input"] = args
        return ev


def aux_summary(dump: dict) -> str:
    """生成辅助调用摘要（写入 run.notes）。"""
    aux = dump.get("aux_requests") or []
    if not aux:
        return ""
    tokens = sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in aux)
    keys = sorted({str(r.get("model_key")) for r in aux})
    return f"剔除 {len(aux)} 次辅助调用（model_key={','.join(keys)}，共 {tokens} tokens，如标题生成）"
