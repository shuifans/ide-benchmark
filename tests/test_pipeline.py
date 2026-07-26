"""agent-coding-bench 流水线测试。

覆盖：schema 正反例 / prepare / verifier 有效性 / pricing /
claude + qoder + opencode adapter 假日志 / process_metrics 信号 / report 合成逻辑（含质量门槛）。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

import common  # noqa: E402
import pricing  # noqa: E402
import report  # noqa: E402
from base import aggregate_stats, load_events  # noqa: E402
from claude_code import ClaudeCodeAdapter  # noqa: E402
from qoder_cli import QoderCliAdapter  # noqa: E402
from opencode import OpencodeAdapter  # noqa: E402
from process_metrics import compute_signals  # noqa: E402


# ---------- schema 正反例 ----------

def _minimal_run():
    return {
        "run_id": "20260719-py-fix-off-by-one-claude-code-deepseek-v4-pro-01",
        "task_id": "py-fix-off-by-one",
        "agent": {"harness": "claude-code", "model": "deepseek-v4-pro"},
        "started_at": "2026-07-19T10:00:00+08:00",
        "ended_at": "2026-07-19T10:10:00+08:00",
        "duration_s": 600.0,
        "usage": {
            "input_tokens": 1000, "cache_read_tokens": 500, "output_tokens": 200,
            "cost_usd": 0.005, "cost_source": "price_table", "price_version": "2026-07-v1",
        },
        "status": "completed",
        "prompt_sha256": "a" * 64,
    }


def test_schema_run_ok():
    assert common.validate(_minimal_run(), "run") == []


def test_schema_run_bad():
    bad = _minimal_run()
    bad["usage"]["input_tokens"] = -5
    bad["status"] = "weird"
    errors = common.validate(bad, "run")
    assert any("input_tokens" in e for e in errors)
    assert any("status" in e for e in errors)


def test_schema_manifest_and_score():
    manifest = {
        "manifest_version": "1",
        "run_id": "20260719-py-fix-off-by-one-qoder-cli-deepseek-v4-pro-01",
        "task_id": "py-fix-off-by-one",
        "prompt_version": "v1",
        "prompt_sha256": "b" * 64,
        "prompt_file": "PROMPT.md",
        "agent": "qoder-cli",
        "model": "deepseek-v4-pro",
        "params": {"context_window": 1000000, "thinking_effort": "max"},
        "created_at": "2026-07-19T02:00:00+00:00",
    }
    assert common.validate(manifest, "manifest") == []

    score = {"run_id": "x", "verifier": {"passed": 5, "total": 9, "score": 55.6}}
    assert common.validate(score, "score") == []
    bad_score = {"run_id": "x", "verifier": {"passed": 5, "total": 9, "score": 155.6}}
    assert common.validate(bad_score, "score") != []


# ---------- prepare ----------

def test_prepare_creates_run_dir():
    sys.path.insert(0, str(SCRIPTS))
    import prepare as prep
    guide = prep.prepare("py-fix-off-by-one", "claude-code", "deepseek-v4-pro", "1M", "max")
    run_dir = Path(guide["run_dir"])
    try:
        assert (run_dir / "manifest.json").is_file()
        assert (run_dir / "PROMPT.md").is_file()
        assert (run_dir / "work" / "pagination.py").is_file()
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["params"]["context_window"] == 1000000
        assert manifest["params"]["thinking_effort"] == "max"
        assert manifest["agent"] == "claude-code"
        # seq 自增
        guide2 = prep.prepare("py-fix-off-by-one", "claude-code", "deepseek-v4-pro", "1M", "max")
        assert guide2["run_id"] != guide["run_id"]
        shutil.rmtree(guide2["run_dir"])
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# ---------- verifier 有效性：带 bug 失败、修复后通过 ----------

FIXED_PAGINATION = '''"""fixed"""


def total_pages(total_items: int, page_size: int) -> int:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total_items < 0:
        raise ValueError("total_items must be >= 0")
    return -(-total_items // page_size)


def paginate(items: list, page: int, page_size: int) -> list:
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    start = (page - 1) * page_size
    return items[start:start + page_size]


def page_info(total_items: int, page: int, page_size: int) -> dict:
    pages = total_pages(total_items, page_size)
    return {
        "page": page, "page_size": page_size, "total_items": total_items,
        "total_pages": pages, "has_next": page < pages, "has_prev": page > 1,
    }
'''


def _run_verifier(work: Path) -> subprocess.CompletedProcess:
    shutil.copytree(ROOT / "tasks" / "py-fix-off-by-one" / "verifier", work / "verifier")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "verifier/", "-q", "--tb=no"],
        cwd=work, capture_output=True, text=True, timeout=120,
    )


def test_verifier_fails_on_buggy_and_passes_on_fixed(tmp_path):
    buggy = tmp_path / "buggy"
    shutil.copytree(ROOT / "tasks" / "py-fix-off-by-one" / "workspace", buggy)
    proc = _run_verifier(buggy)
    assert proc.returncode != 0
    assert "5 failed" in proc.stdout and "4 passed" in proc.stdout

    fixed = tmp_path / "fixed"
    shutil.copytree(ROOT / "tasks" / "py-fix-off-by-one" / "workspace", fixed)
    (fixed / "pagination.py").write_text(FIXED_PAGINATION, encoding="utf-8")
    proc2 = _run_verifier(fixed)
    assert proc2.returncode == 0
    assert "9 passed" in proc2.stdout


# ---------- pricing ----------

def test_pricing_cost_and_alias():
    # deepseek-v4-pro: 12/24/2.4 CNY/M → /7.2 USD
    cost = pricing.cost_usd("deepseek-v4-pro", 1_000_000, 1_000_000, 1_000_000)
    assert cost == pytest.approx((12 + 24 + 2.4) / 7.2, rel=1e-3)
    assert pricing.resolve_model("DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert pricing.resolve_model("claude-opus-4-8") == "ultimate"
    assert pricing.cost_usd("no-such-model", 1, 1, 1) is None


# ---------- claude adapter（假日志）----------

def _fake_claude_transcript(path: Path):
    lines = [
        {"type": "user", "timestamp": "2026-07-19T10:00:00.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "修复 pagination.py"}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:00:05.000Z",
         "message": {"role": "assistant", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 100, "output_tokens": 50,
                               "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10},
                     "content": [
                         {"type": "text", "text": "计划：\n1. 阅读 pagination.py\n2. 修复 total_pages\n3. 修复 paginate\n4. 自测"},
                         {"type": "tool_use", "name": "TodoWrite",
                          "input": {"todos": [
                              {"content": "阅读 pagination.py", "status": "completed"},
                              {"content": "修复 total_pages", "status": "completed"},
                              {"content": "修复 paginate", "status": "pending"}]}},
                     ]}},
        {"type": "user", "timestamp": "2026-07-19T10:00:06.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:00:10.000Z",
         "message": {"role": "assistant", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 200, "output_tokens": 80,
                               "cache_read_input_tokens": 60, "cache_creation_input_tokens": 0},
                     "content": [
                         {"type": "tool_use", "name": "Bash", "input": {"command": "python -c \"import pagination\""}},
                     ]}},
        {"type": "user", "timestamp": "2026-07-19T10:00:11.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "boom", "is_error": True}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:00:12.000Z",
         "message": {"role": "assistant", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 0, "output_tokens": 10,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     "content": [
                         {"type": "tool_use", "name": "Bash", "input": {"command": "python -c \"import pagination\""}},
                     ]}},
        {"type": "user", "timestamp": "2026-07-19T10:00:13.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "boom", "is_error": True}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:01:00.000Z",
         "message": {"role": "assistant", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 300, "output_tokens": 120,
                               "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
                     "content": [
                         {"type": "tool_use", "name": "Edit",
                          "input": {"file_path": "pagination.py", "old_string": "a", "new_string": "b"}},
                     ]}},
        {"type": "user", "timestamp": "2026-07-19T10:01:01.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:02:00.000Z",
         "message": {"role": "assistant", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 400, "output_tokens": 150,
                               "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0},
                     "content": [
                         {"type": "tool_use", "name": "Bash", "input": {"command": "python -m pytest -q"}},
                         {"type": "text", "text": "全部测试通过，修复完成。"},
                     ]}},
        {"type": "user", "timestamp": "2026-07-19T10:02:05.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "9 passed"}]}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_claude_adapter_parse_and_aggregate(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _fake_claude_transcript(transcript)
    events = ClaudeCodeAdapter().parse_events(transcript)
    stats = aggregate_stats(events)

    assert stats["input_tokens"] == 1000
    assert stats["output_tokens"] == 410
    assert stats["cache_read_tokens"] == 400
    assert stats["cache_creation_tokens"] == 10
    assert stats["tool_calls"] == 5
    assert stats["failed_tool_calls"] == 2
    assert stats["actual_model"] == "deepseek-v4-pro"
    assert stats["duration_s"] == pytest.approx(125.0, abs=1.0)

    signals = compute_signals(events)
    assert signals["planning"] is True
    assert signals["plan_items"] == 3
    assert signals["plan_completed"] == 2
    assert signals["self_test_after_last_edit"] is True
    assert signals["retry_pattern_max"] == 1
    assert signals["failed_tool_calls"] == 2
    # 计划文本与编辑都涉及 pagination.py → 范围符合度 1.0
    assert signals["scope_adherence"] == 1.0


def test_claude_dedup_multiline_usage(tmp_path):
    """Claude Code 把一条 assistant 消息拆成多行 JSONL，每行带不同 usage 快照；
    同一 message.id 只应计最后一条权威 usage，不能按行累加。"""
    p = tmp_path / "t.jsonl"
    lines = [
        # 第 1 轮：同一 id 两行，thinking 快照 input 虚高、cache_read=0；tool_use 才是真实拆分
        {"type": "assistant", "timestamp": "2026-07-19T10:00:00Z",
         "message": {"id": "mA", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 56079, "output_tokens": 0,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     "content": [{"type": "thinking", "thinking": "..."}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:00:05Z",
         "message": {"id": "mA", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 800, "output_tokens": 100,
                               "cache_read_input_tokens": 29000, "cache_creation_input_tokens": 0},
                     "content": [{"type": "tool_use", "name": "Edit",
                                  "input": {"file_path": "x.py", "old_string": "a", "new_string": "b"}}]}},
        # 第 2 轮：另一 id，同样两行
        {"type": "assistant", "timestamp": "2026-07-19T10:01:00Z",
         "message": {"id": "mB", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 56100, "output_tokens": 0,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     "content": [{"type": "text", "text": "done"}]}},
        {"type": "assistant", "timestamp": "2026-07-19T10:01:05Z",
         "message": {"id": "mB", "model": "deepseek-v4-pro",
                     "usage": {"input_tokens": 500, "output_tokens": 200,
                               "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 0},
                     "content": [{"type": "tool_use", "name": "Bash",
                                  "input": {"command": "pytest"}}]}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        for r in lines:
            f.write(json.dumps(r) + "\n")

    events = ClaudeCodeAdapter().parse_events(p)
    stats = aggregate_stats(events)
    # 只计每个 id 最后一条：input=800+500=1300（而非 56079+800+56100+500）
    assert stats["input_tokens"] == 1300
    assert stats["output_tokens"] == 300
    assert stats["cache_read_tokens"] == 59000
    # 工具调用仍应完整保留（不因去重而丢）
    assert stats["tool_calls"] == 2
    # turns 按带 usage 的 assistant 消息数 = 2（每个 id 一次）
    assert stats["turns"] == 2


def test_process_signals_negative_sample():
    # 无计划、无自测：上来就编辑，之后没有任何命令
    events = [
        {"ts": None, "type": "assistant_message", "text": "好的"},
        {"ts": None, "type": "tool_call", "name": "Edit", "tool_class": "edit",
         "files": ["x.py"]},
    ]
    signals = compute_signals(events)
    assert signals["planning"] is False
    assert signals["self_test_after_last_edit"] is False
    assert signals["self_test_kind"] is None
    assert signals["retry_pattern_max"] == 0


def test_process_signals_adhoc_self_test():
    # 编辑后用 python -c 内联 assert 自测 → adhoc
    events = [
        {"type": "tool_call", "name": "Edit", "tool_class": "edit", "files": ["pagination.py"]},
        {"type": "tool_call", "name": "Bash", "tool_class": "command",
         "command": "python -c \"from pagination import total_pages; assert total_pages(10, 3) == 4\""},
    ]
    signals = compute_signals(events)
    assert signals["self_test_after_last_edit"] is True
    assert signals["self_test_kind"] == "adhoc"
    # 编辑后仅做语法检查（无 assert）→ 不算自测
    events2 = [
        {"type": "tool_call", "name": "Edit", "tool_class": "edit", "files": ["pagination.py"]},
        {"type": "tool_call", "name": "Bash", "tool_class": "command",
         "command": "python -c \"import pagination\""},
    ]
    assert compute_signals(events2)["self_test_after_last_edit"] is False


# ---------- qoder adapter（假 trace DB + dump）----------

def _sse_body(chunks: list[dict]) -> str:
    """构造 claude-tap 抓取的 qoder SSE 响应体（二次编码 JSON）。"""
    return "\n".join("data:" + json.dumps({"body": json.dumps(c, ensure_ascii=False)})
                     for c in chunks)


def _tap_record(idx: int, path: str, body, model_key: str | None, ts: str):
    headers = {"X-Model-Key": model_key} if model_key else {}
    return (f"s1", idx, idx, ts, json.dumps({
        "timestamp": ts, "duration_ms": 3000,
        "request": {"method": "POST", "path": path, "headers": headers},
        "response": {"status": 200, "body": body},
    }, ensure_ascii=False))


def test_qoder_adapter_sqlite_extraction(tmp_path):
    db = tmp_path / "traces.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, started_at TEXT, updated_at TEXT, client TEXT)")
    con.execute("INSERT INTO sessions VALUES ('s1','2026-07-19T03:43:57+00:00','2026-07-19T03:49:14+00:00','qoder')")
    con.execute("INSERT INTO sessions VALUES ('s0','2026-06-01T00:00:00+00:00','2026-06-01T00:01:00+00:00','claude')")
    con.execute("CREATE TABLE records (session_id TEXT, record_index INTEGER, turn INTEGER, timestamp TEXT, payload_json TEXT)")

    llm_path = "/algo/api/v2/service/pro/sse/agent_chat_generation"
    # 非 LLM 控制面记录
    con.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                _tap_record(1, "/api/v1/userinfo", {"id": "u"}, None, "2026-07-19T03:44:00+00:00"))
    # 主模型 LLM 记录：content + tool_calls + usage（带缓存明细）
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "我来修复"}, "index": 0}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
            "name": "Edit", "arguments": "{\"file_path\": \"pagination.py\"}"}}]}, "index": 0}]},
        {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1100, "prompt_tokens_details": {"cached_tokens": 100},
                   "completion_tokens": 200, "total_tokens": 1300}},
    ]
    con.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                _tap_record(2, llm_path, _sse_body(chunks), "dmodel", "2026-07-19T03:44:48+00:00"))
    # 辅助调用（lite，标题生成）——应从汇总剔除
    aux_chunks = [{"choices": [{"delta": {"content": "标题"}, "index": 0}],
                   "usage": {"prompt_tokens": 50, "completion_tokens": 5}}]
    con.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                _tap_record(3, llm_path, _sse_body(aux_chunks), "lite", "2026-07-19T03:45:37+00:00"))
    con.commit()
    con.close()

    adapter = QoderCliAdapter()
    dump = adapter._extract_session(db, "2026-07-19T03:40:00+00:00")
    assert dump["session_id"] == "s1"
    assert dump["main_model_key"] == "dmodel"
    assert len(dump["requests"]) == 1
    assert len(dump["aux_requests"]) == 1

    tap_json = tmp_path / "transcript.tap.json"
    tap_json.write_text(json.dumps(dump), encoding="utf-8")
    events = adapter.parse_events(tap_json)
    stats = aggregate_stats(events)
    # 只汇总主模型：input = 1100-100, cache_read = 100, output = 200
    assert stats["input_tokens"] == 1000
    assert stats["cache_read_tokens"] == 100
    assert stats["output_tokens"] == 200
    assert stats["tool_calls"] == 1
    assert stats["actual_model"] == "dmodel"


# ---------- opencode adapter（假 opencode.db）----------

def _oc_msg(role: str, tokens: dict | None = None, model_id: str | None = None):
    d = {"role": role}
    if role == "assistant":
        d["tokens"] = tokens or {}
        d["modelID"] = model_id
    return json.dumps(d, ensure_ascii=False)


def _oc_part(ptype: str, **kw) -> str:
    d = {"type": ptype}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def test_opencode_adapter_sqlite_extraction(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    work_dir_norm = str(work.resolve()).replace("\\", "/")

    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE session (id TEXT, directory TEXT, agent TEXT, model TEXT, cost REAL,"
        " tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,"
        " tokens_cache_read INTEGER, tokens_cache_write INTEGER,"
        " time_created INTEGER, time_updated INTEGER)")
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")

    # 干扰项：别的目录的 session（不应被选中）
    con.execute("INSERT INTO session VALUES ('ses_other','C:/other','build','{\"id\":\"x\"}',0,1,1,0,0,0,1000,2000)")
    # 目标 session：两步，含缓存与 reasoning
    model_json = '{"id":"deepseek-v4-pro","providerID":"bailian"}'
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ses_t", work_dir_norm, "build", model_json, 0.0,
                 3000, 200, 50, 1500, 0, 1_784_420_000_000, 1_784_420_050_000))
    con.execute("INSERT INTO message VALUES ('m0','ses_t',1784420000000,?)",
                (_oc_msg("user"),))
    con.execute("INSERT INTO message VALUES ('m1','ses_t',1784420010000,?)",
                (_oc_msg("assistant", {"input": 1000, "output": 80, "reasoning": 20,
                                       "cache": {"read": 500, "write": 0}}, "deepseek-v4-pro"),))
    con.execute("INSERT INTO message VALUES ('m2','ses_t',1784420040000,?)",
                (_oc_msg("assistant", {"input": 2000, "output": 120, "reasoning": 30,
                                       "cache": {"read": 1000, "write": 0}}, "deepseek-v4-pro"),))
    con.execute("INSERT INTO part VALUES ('p0','m0','ses_t',1784420000000,?)",
                (_oc_part("text", text="修复 pagination.py"),))
    con.execute("INSERT INTO part VALUES ('p1','m1','ses_t',1784420010000,?)",
                (_oc_part("tool", tool="edit", callID="c1",
                          state={"status": "completed", "input": {"filePath": "pagination.py"}}),))
    con.execute("INSERT INTO part VALUES ('p2','m2','ses_t',1784420040000,?)",
                (_oc_part("tool", tool="bash", callID="c2",
                          state={"status": "completed",
                                 "input": {"command": "python -c \"import pagination; assert True\""}}),))
    con.commit()
    con.close()

    adapter = OpencodeAdapter()
    dump = adapter._extract_session(db, work_dir_norm, "2026-07-19T00:00:00+00:00")
    assert dump["session_id"] == "ses_t"
    assert dump["model"] == "deepseek-v4-pro"
    assert len(dump["messages"]) == 3

    tap = tmp_path / "transcript.opencode.json"
    tap.write_text(json.dumps(dump), encoding="utf-8")
    events = adapter.parse_events(tap)
    stats = aggregate_stats(events)
    # input 非缓存直接求和； output = output + reasoning； cache_read 直接
    assert stats["input_tokens"] == 3000
    assert stats["output_tokens"] == 250   # (80+20)+(120+30)
    assert stats["cache_read_tokens"] == 1500
    assert stats["tool_calls"] == 2
    assert stats["actual_model"] == "deepseek-v4-pro"
    # 逐消息汇总与 session 聚合一致（input/cache_read）
    assert stats["input_tokens"] == dump["session_tokens"]["input"]
    assert stats["cache_read_tokens"] == dump["session_tokens"]["cache_read"]


def test_opencode_no_matching_session(tmp_path):
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE session (id TEXT, directory TEXT, agent TEXT, model TEXT, cost REAL,"
        " tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,"
        " tokens_cache_read INTEGER, tokens_cache_write INTEGER,"
        " time_created INTEGER, time_updated INTEGER)")
    con.execute("INSERT INTO session VALUES ('s','C:/somewhere/else','build','{\"id\":\"x\"}',0,1,1,0,0,0,1000,2000)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="未找到"):
        OpencodeAdapter()._extract_session(db, "c:/no/such/work", None)


# ---------- report 合成逻辑 ----------

def _write_fixture(base: Path, run_id: str, task: str, harness: str, model: str,
                   tokens: int, duration: float, verifier_passed: int, verifier_total: int,
                   judge_scores: dict | None, status: str = "completed"):
    runs_dir = base / "runs"
    scores_dir = base / "scores"
    runs_dir.mkdir(parents=True, exist_ok=True)
    scores_dir.mkdir(parents=True, exist_ok=True)
    run = _minimal_run()
    run.update({
        "run_id": run_id, "task_id": task,
        "agent": {"harness": harness, "model": model, "params": {"context_window": 1000000, "thinking_effort": "max"}},
        "duration_s": duration, "status": status,
    })
    run["usage"].update({
        "input_tokens": tokens // 2, "cache_read_tokens": tokens // 4,
        "output_tokens": tokens // 4,
        "cost_usd": round(tokens / 1e6 * 2.0, 6),
    })
    (runs_dir / f"{run_id}.run.json").write_text(
        json.dumps(run, ensure_ascii=False), encoding="utf-8")
    score = {
        "run_id": run_id, "task_id": task,
        "verifier": {"passed": verifier_passed, "total": verifier_total,
                     "score": round(verifier_passed / verifier_total * 100, 1)},
    }
    if judge_scores:
        score["judge"] = {"model": "judge-x", "rubric_version": "v2", "blind": True,
                          "scores": judge_scores}
    (scores_dir / f"{run_id}.score.json").write_text(
        json.dumps(score, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def report_dirs(tmp_path, monkeypatch):
    base = tmp_path / "results"
    monkeypatch.setattr(report, "RESULTS_RUNS_DIR", base / "runs")
    monkeypatch.setattr(report, "RESULTS_SCORES_DIR", base / "scores")
    return base


JUDGE_A = {"code_quality": 80, "planning": 90, "discipline": 85, "self_test": 95, "comments": "a"}
JUDGE_B = {"code_quality": 70, "planning": 60, "discipline": 65, "self_test": 50, "comments": "b"}


def test_report_median_normalization_and_gate(report_dirs, tmp_path):
    # combo1（claude）：3 次，含一次 verifier 全挂（触发质量门槛）
    _write_fixture(report_dirs, "r-c1-01", "t1", "claude-code", "m1", 1000, 100, 9, 9, JUDGE_A)
    _write_fixture(report_dirs, "r-c1-02", "t1", "claude-code", "m1", 1200, 120, 9, 9, JUDGE_A)
    _write_fixture(report_dirs, "r-c1-03", "t1", "claude-code", "m1", 800, 80, 0, 9, JUDGE_A)
    # combo2（qoder）：3 次
    _write_fixture(report_dirs, "r-c2-01", "t1", "qoder-cli", "m1", 3000, 300, 7, 9, JUDGE_B)
    _write_fixture(report_dirs, "r-c2-02", "t1", "qoder-cli", "m1", 3200, 320, 7, 9, JUDGE_B)
    _write_fixture(report_dirs, "r-c2-03", "t1", "qoder-cli", "m1", 2800, 280, 7, 9, JUDGE_B)
    run_ids = [f"r-c1-0{i}" for i in (1, 2, 3)] + [f"r-c2-0{i}" for i in (1, 2, 3)]

    rows = report.compute_rows(run_ids, dict(report.DEFAULT_WEIGHTS))
    by_id = {r["run_id"]: r for r in rows}

    # 质量门槛：verifier 全挂的 r-c1-03 效率分 0
    assert by_id["r-c1-03"]["t_score"] == 0.0
    assert by_id["r-c1-03"]["d_score"] == 0.0
    assert by_id["r-c1-03"]["gated"] is False
    assert by_id["r-c1-01"]["gated"] is True

    # 归一化：claude 组 token 最低（800 在 r-c1-03，但被门槛清零）；qoder token 高 → 归一化低分
    assert by_id["r-c1-01"]["token_norm"] > by_id["r-c2-01"]["token_norm"]

    # 综合分：claude 前两次 > qoder（结果分与 judge 分都高）
    assert by_id["r-c1-01"]["composite"] > by_id["r-c2-01"]["composite"]

    # 中位数：claude 组综合分中位数 = 排序后中间值
    groups = report.group_medians(rows)
    g1 = next(g for g in groups if g["harness"] == "claude-code")
    g2 = next(g for g in groups if g["harness"] == "qoder-cli")
    assert g1["n"] == 3 and g2["n"] == 3
    comps = sorted(r["composite"] for r in rows if r["run"]["agent"]["harness"] == "claude-code")
    assert g1["composite_med"] == round(comps[1], 2)
    assert g1["composite_min"] == round(comps[0], 2)
    assert g1["composite_max"] == round(comps[2], 2)

    # compare HTML 产出
    out = report.render_compare(run_ids, dict(report.DEFAULT_WEIGHTS), tmp_path)
    text = out.read_text(encoding="utf-8")
    assert "对比报告" in text and "r-c1-01" in text
    assert "效率清零" in text  # 门槛标记


def test_report_single(report_dirs, tmp_path):
    _write_fixture(report_dirs, "r-s-01", "t1", "claude-code", "m1", 1000, 100, 9, 9, JUDGE_A)
    out = report.render_single("r-s-01", tmp_path)
    text = out.read_text(encoding="utf-8")
    assert "单 Agent 运行分析" in text
    assert "deepseek" not in text  # 该 fixture 模型为 m1
    assert "r-s-01" in text and "Judge 盲评" in text


def test_validate_all_cli():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate.py"), "--all"],
        capture_output=True, text=True, timeout=60,
    )
    # results/ 为空时退出码 2；有文件时 0/1。当前阶段为空。
    assert proc.returncode in (0, 1, 2)
