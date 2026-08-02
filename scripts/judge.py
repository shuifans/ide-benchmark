"""L2 judge：调用 OpenAI 兼容 API 对 run 盲评（rubric-v3）并生成对比分析正文。

用法：
  python scripts/judge.py run <run_id> [--force]
  python scripts/judge.py compare <run_id_a> <run_id_b> ... [--force]

配置：config/judge.json（参考 judge.example.json）：
  {"base_url": "...", "api_key": "...", "model": "...", "temperature": 0}
环境变量 JUDGE_API_KEY / JUDGE_BASE_URL / JUDGE_MODEL 可覆盖。

盲评保障：材料中的 agent/厂商标识符统一替换为 [agent]；对比分析用 候选A/B/C 标签，
标签 → run_id 映射与正文一起存 results/analysis/，报告渲染时再去匿名。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from common import (
    CONFIG_DIR, RESULTS_RUNS_DIR, RESULTS_SCORES_DIR, ROOT, RUNS_DIR, TASKS_DIR,
    dump_json, is_judge_only_verifier, load_json, validate, eprint,
)

RUBRIC_PATH = CONFIG_DIR / "rubric-v3.md"
RUBRIC_VERSION = "v3.1"
ANALYSIS_DIR = ROOT / "results" / "analysis"

# 盲评清洗：agent / 厂商 / 模型家族标识符 → [agent]
_IDENTIFIER_RE = re.compile(
    r"\b(claude[\w.-]*|anthropic|qoder(cli)?|opencode|codex|openai|gpt-[\w.-]+|"
    r"qwen[\w.-]*|dashscope|tongyi|kimi[\w.-]*|moonshot|deepseek[\w.-]*|"
    r"glm[\w.-]*|zhipu|minimax[\w.-]*|gemini[\w.-]*|pi)\b",
    re.IGNORECASE,
)

DIFF_MAX_CHARS = 15000
SUMMARY_MAX_CHARS = 10000


def anonymize(text: str) -> str:
    return _IDENTIFIER_RE.sub("[agent]", text or "")


def load_config() -> dict:
    cfg_path = CONFIG_DIR / "judge.json"
    cfg = load_json(cfg_path) if cfg_path.is_file() else {}
    cfg["base_url"] = os.environ.get("JUDGE_BASE_URL") or cfg.get("base_url")
    cfg["api_key"] = os.environ.get("JUDGE_API_KEY") or cfg.get("api_key")
    cfg["model"] = os.environ.get("JUDGE_MODEL") or cfg.get("model")
    cfg.setdefault("temperature", 0)
    missing = [k for k in ("base_url", "api_key", "model") if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"judge 配置缺失 {missing}：请复制 config/judge.example.json 为 config/judge.json 填写，"
            f"或设置 JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL 环境变量"
        )
    return cfg


def chat(cfg: dict, messages: list[dict], json_mode: bool = False) -> tuple[str, dict]:
    """调 chat/completions，返回 (content, usage)。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}
    return content, usage


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"judge 输出无法解析为 JSON: {text[:300]}")


def _work_diff(run_id: str, task_id: str) -> str:
    """最终代码 vs 任务初始 workspace 的 diff（排除 verifier/缓存）。"""
    work = RUNS_DIR / run_id / "work"
    base = TASKS_DIR / task_id / "workspace"
    if not work.is_dir() or not base.is_dir():
        return "(diff 不可用)"
    try:
        out = subprocess.run(
            ["diff", "-ruN",
             "-x", "verifier", "-x", "__pycache__", "-x", ".pytest_cache", "-x", ".git",
             str(base), str(work)],
            capture_output=True, text=True, timeout=60,
        )
        text = out.stdout or "(无改动)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(diff 失败: {exc})"
    text = text.replace(str(base), "a").replace(str(work), "b")
    if len(text) > DIFF_MAX_CHARS:
        text = text[:DIFF_MAX_CHARS] + "\n...（截断）..."
    return text


def _gather_materials(run_id: str) -> dict:
    run_dir = RUNS_DIR / run_id
    score_path = RESULTS_SCORES_DIR / f"{run_id}.score.json"
    if not score_path.is_file():
        raise FileNotFoundError(f"缺少 score.json（请先跑 verify + process_metrics）: {score_path}")
    score = load_json(score_path)
    manifest = load_json(run_dir / "manifest.json")
    task_id = manifest["task_id"]

    summary_path = run_dir / "transcript_summary.txt"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else "(无摘要)"
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS] + "\n...（截断）..."

    prompt_path = TASKS_DIR / task_id / "prompt.md"
    task_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
    import yaml
    with open(TASKS_DIR / task_id / "task.yaml", "r", encoding="utf-8") as f:
        task_meta = yaml.safe_load(f)

    return {
        "score": score,
        "task_id": task_id,
        "difficulty": task_meta.get("difficulty", "easy"),
        "task_prompt": task_prompt,
        "summary": summary,
        "diff": _work_diff(run_id, task_id),
    }


def _judge_prompt(mat: dict) -> str:
    rubric = RUBRIC_PATH.read_text(encoding="utf-8")
    verifier = mat["score"].get("verifier") or {}
    signals = mat["score"].get("process_signals") or {}
    if is_judge_only_verifier(verifier):
        verifier_section = ("## verifier 结果（客观检查）\n\n"
                            "本任务无客观验证器（judge-only 任务）：没有 L0 测试通过率，"
                            "请以【任务提示词】中的显式要求为唯一验收标准，"
                            "据此评判产出物的完成度与质量。\n\n")
    else:
        verifier_section = ("## verifier 结果（客观检查）\n\n"
                            f"通过 {verifier.get('passed')}/{verifier.get('total')}，"
                            f"得分 {verifier.get('score')}\n\n")
    return anonymize(
        f"{rubric}\n\n"
        f"---\n\n"
        f"# 待评 run 材料（任务档位：{mat['difficulty']}）\n\n"
        f"## 任务提示词\n\n{mat['task_prompt']}\n\n"
        f"## transcript 摘要\n\n{mat['summary']}\n\n"
        f"## L1 过程信号\n\n```json\n{json.dumps(signals, ensure_ascii=False, indent=2)}\n```\n\n"
        f"{verifier_section}"
        f"## 最终代码 diff\n\n```diff\n{mat['diff']}\n```\n\n"
        f"---\n\n"
        f"请严格按 rubric v3 打分。只输出一个 JSON 对象，形如：\n"
        f'{{"code_quality": 0, "planning": 0, "discipline": 0, "self_test": 0, '
        f'"comments": "...", "flags": []}}'
    )


def _validate_scores(scores: dict) -> dict:
    out = {}
    for dim in ("code_quality", "planning", "discipline", "self_test"):
        v = scores.get(dim)
        if not isinstance(v, (int, float)) or not 0 <= v <= 100:
            raise ValueError(f"judge 维度 {dim} 非法: {v!r}")
        out[dim] = round(float(v), 1)
    out["comments"] = str(scores.get("comments") or "")
    flags = scores.get("flags") or []
    out["flags"] = [str(x) for x in flags] if isinstance(flags, list) else []
    return out


def judge_run(run_id: str, force: bool = False) -> dict:
    """盲评一条 run，judge 段写回 score.json。已有 judge 且非 force 时跳过。"""
    score_path = RESULTS_SCORES_DIR / f"{run_id}.score.json"
    score = load_json(score_path)
    if score.get("judge") and not force:
        return score["judge"]

    cfg = load_config()
    mat = _gather_materials(run_id)
    prompt = _judge_prompt(mat)
    messages = [
        {"role": "system",
         "content": "你是严格的代码评审 judge。按给定 rubric 盲评，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]

    last_err = None
    for attempt in range(2):
        try:
            content, usage = chat(cfg, messages, json_mode=True)
            scores = _validate_scores(_extract_json(content))
            break
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            last_err = exc
            messages.append({"role": "user",
                             "content": f"输出解析失败（{exc}），请只输出合法 JSON 对象。"})
    else:
        raise RuntimeError(f"judge 两次输出均无法解析: {last_err}")

    judge = {
        "model": cfg["model"],
        "rubric_version": RUBRIC_VERSION,
        "blind": True,
        "scores": scores,
        "judge_usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }
    score["judge"] = judge
    errors = validate(score, "score")
    if errors:
        raise RuntimeError("score.json 未通过 schema 校验: " + "; ".join(errors))
    dump_json(score, score_path)
    return judge


def _candidate_row(run_id: str, label: str) -> dict:
    run = load_json(RESULTS_RUNS_DIR / f"{run_id}.run.json")
    score = load_json(RESULTS_SCORES_DIR / f"{run_id}.score.json")
    verifier = score.get("verifier") or {}
    judge = (score.get("judge") or {}).get("scores") or {}
    signals = score.get("process_signals") or {}
    usage = run.get("usage") or {}
    return {
        "label": label,
        "verifier": ("无（judge-only 任务，无客观验证器，以提示词要求为准）"
                     if is_judge_only_verifier(verifier)
                     else f"{verifier.get('passed')}/{verifier.get('total')}（{verifier.get('score')}分）"),
        "judge_scores": {k: judge.get(k) for k in
                         ("code_quality", "planning", "discipline", "self_test")},
        "judge_comments": judge.get("comments", ""),
        "flags": judge.get("flags") or [],
        "total_tokens": (usage.get("input_tokens", 0) + usage.get("cache_read_tokens", 0)
                         + usage.get("cache_creation_tokens", 0) + usage.get("output_tokens", 0)),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": usage.get("cost_usd"),
        "duration_s": run.get("duration_s"),
        "tool_calls": run.get("tool_calls"),
        "turns": run.get("turns"),
        "status": run.get("status"),
        "signals": {k: signals.get(k) for k in
                    ("planning", "plan_items", "plan_completed", "scope_adherence",
                     "self_test_after_last_edit", "self_test_kind",
                     "retry_pattern_max", "failed_tool_calls")},
    }


def compare_key(run_ids: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(run_ids)).encode()).hexdigest()[:16]


def judge_compare(run_ids: list[str], force: bool = False) -> dict:
    """生成匿名对比分析正文，缓存于 results/analysis/<hash>.json。"""
    key = compare_key(run_ids)
    out_path = ANALYSIS_DIR / f"{key}.json"
    if out_path.is_file() and not force:
        return load_json(out_path)

    cfg = load_config()
    labels = [f"候选{chr(ord('A') + i)}" for i in range(len(run_ids))]
    rows = [_candidate_row(rid, lab) for rid, lab in zip(run_ids, labels)]
    task_id = load_json(RESULTS_RUNS_DIR / f"{run_ids[0]}.run.json")["task_id"]
    prompt_path = TASKS_DIR / task_id / "prompt.md"
    task_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""

    prompt = anonymize(
        f"以下是同一编码任务上多位匿名候选（编码 agent 工具）的测评数据。\n\n"
        f"## 任务提示词\n\n{task_prompt}\n\n"
        f"## 各候选数据\n\n```json\n{json.dumps(rows, ensure_ascii=False, indent=2)}\n```\n\n"
        f"指标说明：verifier=客观测试通过情况（L0，judge-only 任务无验证器，以提示词要求为准）；"
        f"judge_scores=盲评四维 0-100（L2）；"
        f"signals=从执行日志提取的客观过程信号（L1）；total_tokens/cost_usd/duration_s=效率成本。\n\n"
        f"请写一篇约 500-700 字的中文对比分析，使用 markdown，分四节：\n"
        f"### 结果差异\n### 过程风格差异\n### 效率取舍\n### 结论与建议\n\n"
        f"要求：结论必须引用具体数据支撑；指出各候选最突出的优势与短板；"
        f"不要臆测候选身份；直接输出正文，不要额外说明。"
    )
    content, usage = chat(cfg, [
        {"role": "system", "content": "你是资深工程效能分析师，基于数据写客观对比分析。"},
        {"role": "user", "content": prompt},
    ])

    result = {
        "key": key,
        "run_ids": run_ids,
        "labels": {lab: rid for lab, rid in zip(labels, run_ids)},
        "judge_model": cfg["model"],
        "rubric_version": RUBRIC_VERSION,
        "analysis_md": content.strip(),
        "judge_usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }
    dump_json(result, out_path)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="L2 judge 盲评 / 对比分析")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="盲评一条 run")
    p_run.add_argument("run_id")
    p_run.add_argument("--force", action="store_true")
    p_cmp = sub.add_parser("compare", help="生成对比分析正文")
    p_cmp.add_argument("run_ids", nargs="+")
    p_cmp.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        if args.cmd == "run":
            judge = judge_run(args.run_id, force=args.force)
            print(json.dumps(judge, ensure_ascii=False, indent=2))
        else:
            result = judge_compare(args.run_ids, force=args.force)
            print(result["analysis_md"])
    except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
        eprint(f"judge 失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
