"""ide-benchmark Web 向导服务（stdlib，零新依赖）。

用法：python web/server.py [--port 8321]
浏览器打开 http://127.0.0.1:8321/ 按 5 步向导操作。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "adapters"))

from common import AGENTS, REPORTS_DIR, RUNS_DIR, list_tasks, load_json  # noqa: E402
import prepare as prepare_mod  # noqa: E402
import collect as collect_mod  # noqa: E402
import verify as verify_mod  # noqa: E402
import process_metrics as pm_mod  # noqa: E402
import report as report_mod  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 报告后台任务表：job_id -> {state, progress, report_url, error}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def agent_status() -> list[dict]:
    out = []
    for agent in AGENTS:
        binary = {"claude-code": "claude", "qoder-cli": "qodercli"}.get(agent, agent)
        installed = shutil.which(binary) is not None
        guide = prepare_mod.LAUNCH_GUIDE.get(agent, {})
        notes = []
        if not installed:
            notes.append(f"未检测到 {binary}，请先安装")
        if agent == "qoder-cli" and shutil.which("claude-tap") is None:
            notes.append("未检测到 claude-tap：qoder-cli 必须经 claude-tap 包裹否则采不到 token")
        out.append({
            "agent": agent,
            "installed": installed,
            "launch_command": guide.get("command"),
            "launch_note": guide.get("note"),
            "warnings": notes,
        })
    return out


def list_runs() -> list[dict]:
    runs = []
    if not RUNS_DIR.is_dir():
        return runs
    for child in sorted(RUNS_DIR.iterdir()):
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        runs.append({
            "run_id": manifest.get("run_id", child.name),
            "task_id": manifest.get("task_id"),
            "agent": manifest.get("agent"),
            "model": manifest.get("model"),
            "params": manifest.get("params"),
            "created_at": manifest.get("created_at"),
            "collected": (ROOT / "results" / "runs" / f"{child.name}.run.json").is_file(),
            "scored": (ROOT / "results" / "scores" / f"{child.name}.score.json").is_file(),
        })
    return runs


def do_prepare(payload: dict) -> list[dict]:
    task_id = payload["task_id"]
    results = []
    for cand in payload.get("candidates") or []:
        try:
            guide = prepare_mod.prepare(
                task_id, cand["agent"], cand["model"],
                str(cand.get("context_window") or "1M"),
                str(cand.get("thinking") or "default"),
            )
            prompt_text = Path(guide["prompt_file"]).read_text(encoding="utf-8")
            results.append({"ok": True, "prompt_text": prompt_text, **guide})
        except Exception as exc:  # noqa: BLE001 —— 逐候选上报错误
            results.append({"ok": False, "agent": cand.get("agent"),
                            "model": cand.get("model"), "error": str(exc)})
    return results


def do_collect(payload: dict) -> list[dict]:
    results = []
    for run_id in payload.get("run_ids") or []:
        entry = {"run_id": run_id, "ok": False}
        stage = "collect"
        try:
            collect_mod.collect(run_id, None, "completed", 0, None)
            stage = "verify"
            verify_mod.verify(run_id)
            stage = "process_metrics"
            pm_mod.run_metrics(run_id)
            run = load_json(ROOT / "results" / "runs" / f"{run_id}.run.json")
            score = load_json(ROOT / "results" / "scores" / f"{run_id}.score.json")
            u = run.get("usage") or {}
            entry.update({
                "ok": True,
                "tokens": (u.get("input_tokens", 0) + u.get("cache_read_tokens", 0)
                           + u.get("cache_creation_tokens", 0) + u.get("output_tokens", 0)),
                "cost_usd": u.get("cost_usd"),
                "duration_s": run.get("duration_s"),
                "verifier": {"passed": (score.get("verifier") or {}).get("passed"),
                             "total": (score.get("verifier") or {}).get("total")},
            })
        except Exception as exc:  # noqa: BLE001
            entry.update({"stage_failed": stage, "error": str(exc)})
        results.append(entry)
    return results


def _report_job(job_id: str, run_ids: list[str], weights_text: str | None) -> None:
    def set_state(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)
    try:
        import judge as judge_mod
        weights = report_mod.parse_weights(weights_text)
        # 逐 run 盲评（已评的自动跳过）
        for i, rid in enumerate(run_ids):
            set_state(progress=f"judge 盲评 {i + 1}/{len(run_ids)}: {rid}")
            judge_mod.judge_run(rid)
        set_state(progress="生成对比分析正文")
        judge_mod.judge_compare(run_ids)
        set_state(progress="渲染 HTML 报告")
        out = report_mod.render_compare(run_ids, weights, REPORTS_DIR)
        set_state(state="done", progress="完成", report_url=f"/reports/{out.name}")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        set_state(state="error", error=str(exc))


def do_report(payload: dict) -> dict:
    run_ids = payload.get("run_ids") or []
    if not run_ids:
        raise ValueError("run_ids 为空")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"state": "running", "progress": "启动中"}
    t = threading.Thread(target=_report_job,
                         args=(job_id, run_ids, payload.get("weights")), daemon=True)
    t.start()
    return {"job_id": job_id}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            name = Path(path[len("/static/"):]).name  # 防目录穿越
            ct = {"css": "text/css", "js": "application/javascript"}.get(
                name.rsplit(".", 1)[-1], "application/octet-stream")
            self._file(STATIC_DIR / name, f"{ct}; charset=utf-8")
        elif path.startswith("/reports/"):
            name = Path(path[len("/reports/"):]).name
            self._file(REPORTS_DIR / name, "text/html; charset=utf-8")
        elif path == "/api/tasks":
            self._json(list_tasks())
        elif path == "/api/agents":
            self._json(agent_status())
        elif path == "/api/runs":
            self._json(list_runs())
        elif path.startswith("/api/report/status/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            self._json(job or {"state": "error", "error": "未知 job"},
                       200 if job else 404)
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return
        try:
            if path == "/api/prepare":
                self._json(do_prepare(payload))
            elif path == "/api/collect":
                self._json(do_collect(payload))
            elif path == "/api/report":
                self._json(do_report(payload))
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)


def main() -> int:
    ap = argparse.ArgumentParser(description="ide-benchmark Web 向导")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；局域网访问用 0.0.0.0（服务无鉴权，勿暴露到不可信网络）")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ide-benchmark 向导已启动: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
