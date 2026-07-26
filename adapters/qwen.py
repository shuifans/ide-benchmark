"""qwen (qwen-code) adapter：解析 ~/.qwen/projects 下的会话 JSONL。

日志位置：~/.qwen/projects/<escaped-cwd>/chats/<sessionId>.jsonl
escaped-cwd 规则：绝对路径非字母数字字符替换为 "-"（同 claude 系 slug）。
侧车文件 <sessionId>.runtime.json 含 work_dir，用于精确归属校验。

usage 位于 assistant 行顶层 usageMetadata：
  promptTokenCount（含缓存）/ candidatesTokenCount / thoughtsTokenCount / cachedContentTokenCount
归一化：input = prompt - cached；output = candidates + thoughts；cache_read = cached。
工具调用为 Gemini 风格 parts：functionCall {id,name,args} / functionResponse {id,name,response}。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from base import Adapter, classify_tool, parse_ts

QWEN_PROJECTS_DIR = Path.home() / ".qwen" / "projects"


def work_dir_slug(work_dir: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(work_dir.resolve()))


def _runtime_work_dir(chat_file: Path) -> str | None:
    sidecar = chat_file.with_suffix(".runtime.json")
    if not sidecar.is_file():
        sidecar = chat_file.parent / (chat_file.stem + ".runtime.json")
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            return json.load(f).get("work_dir")
    except (OSError, json.JSONDecodeError):
        return None


class QwenAdapter(Adapter):
    name = "qwen"

    def locate_session(self, run_dir: Path, manifest: dict, transcript_override: str | None) -> Path:
        dest = run_dir / "transcript.jsonl"
        if transcript_override:
            shutil.copy2(transcript_override, dest)
            return dest

        work_dir = (run_dir / "work").resolve()
        chats_dir = QWEN_PROJECTS_DIR / work_dir_slug(work_dir) / "chats"
        if not chats_dir.is_dir():
            raise FileNotFoundError(
                f"找不到 qwen 会话目录: {chats_dir}\n"
                f"请确认是在 {work_dir} 下运行的 qwen；或用 --transcript 手动指定"
            )
        candidates = sorted(chats_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        # 侧车 work_dir 精确匹配优先
        exact = [p for p in candidates if _runtime_work_dir(p) == str(work_dir)]
        if exact:
            candidates = exact
        if not candidates:
            raise FileNotFoundError(f"{chats_dir} 下没有会话 JSONL")
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
                rtype = rec.get("type")
                ts = rec.get("timestamp")
                msg = rec.get("message") or {}
                parts = msg.get("parts") or []

                if rtype == "tool_result":
                    tcr = rec.get("toolCallResult") or {}
                    status = str(tcr.get("status") or "").lower()
                    text = str(tcr.get("resultDisplay") or "")
                    if not text:
                        for p in parts:
                            if isinstance(p, dict) and "functionResponse" in p:
                                resp = p["functionResponse"].get("response") or {}
                                text = str(resp.get("output") or resp.get("error") or "")
                                if not status:
                                    status = "error" if ("error" in resp and "output" not in resp) else "success"
                                break
                    events.append({
                        "ts": ts, "type": "tool_result",
                        "status": "ok" if status in ("success", "ok", "") else "error",
                        "text": text[:2000],
                    })

                elif rtype == "user":
                    texts = []
                    for p in parts:
                        if isinstance(p, dict) and p.get("text") and not p.get("thought"):
                            texts.append(p["text"])
                    if texts:
                        events.append({"ts": ts, "type": "user_message",
                                       "text": " ".join(texts).strip()})

                elif rtype == "assistant":
                    usage = self._usage(rec.get("usageMetadata"))
                    model = rec.get("model")
                    emitted = False
                    for p in parts:
                        if not isinstance(p, dict):
                            continue
                        ev = None
                        if "functionCall" in p:
                            ev = self._tool_call_event(ts, p["functionCall"])
                        elif p.get("text") and not p.get("thought"):
                            ev = {"ts": ts, "type": "assistant_message", "text": p["text"]}
                        if ev is not None:
                            if usage and not emitted:
                                ev["usage"] = usage
                                ev["model"] = model
                                emitted = True
                            events.append(ev)
                    if usage and not emitted:
                        events.append({"ts": ts, "type": "assistant_message",
                                       "usage": usage, "model": model})
        return events

    @staticmethod
    def _usage(raw) -> dict | None:
        if not isinstance(raw, dict) or not raw:
            return None
        prompt = int(raw.get("promptTokenCount") or 0)
        cached = int(raw.get("cachedContentTokenCount") or 0)
        u = {
            "input_tokens": max(prompt - cached, 0),
            "output_tokens": int(raw.get("candidatesTokenCount") or 0)
            + int(raw.get("thoughtsTokenCount") or 0),
            "cache_read_tokens": cached,
            "cache_creation_tokens": 0,
        }
        return u if any(u.values()) else None

    @staticmethod
    def _tool_call_event(ts, fc: dict) -> dict:
        name = fc.get("name") or ""
        args = fc.get("args") or {}
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
            for key in ("file_path", "path", "absolute_path", "notebook_path"):
                if args.get(key):
                    files.append(str(args[key]))
            if files:
                ev["files"] = files
            if ev["tool_class"] == "plan":
                ev["plan_input"] = args
        return ev
