"""生成 HTML 报告。

用法：
  python scripts/report.py single <run_id> [-o reports/]
  python scripts/report.py compare <run_id...> [--weights R45,P30,T15,D10] [-o reports/]

综合分公式（默认权重，--weights 可调顶层比例）：
  composite = 0.35*verifier + 0.10*code_quality                 (R 结果 45)
            + 0.10*planning + 0.10*discipline + 0.10*self_test  (P 过程 30)
            + 0.15*token_norm                                   (T token 成本 15)
            + 0.10*duration_norm                                (D 时间成本 10)
  其中 token_norm/duration_norm 为同任务组内 min-max 归一化（越低越好），
  verifier 通过数为 0 或 status 非 completed 的 run，T/D 效率分记 0（质量门槛）。

judge-only 任务（verifier.status == "skipped"，Web 创建的纯提示词自定义任务）：
  verifier 不贡献分数，综合分按可用权重和（默认 0.10+0.30+0.15+0.10 = 65）
  重归一化到 0–100，标 ‡；T/D 门槛仅看 status == completed。
  此类任务不应与带 verifier 的任务混入同一对比。
"""
from __future__ import annotations

import argparse
import html
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

from common import (
    REPORTS_DIR, RESULTS_RUNS_DIR, RESULTS_SCORES_DIR, RUNS_DIR, TASKS_DIR,
    is_judge_only_verifier, load_json, eprint,
)
import pricing

# 顶层权重默认值及子项比例：R 任务结果 / P 过程能力 / T token 成本 / D 时间成本
DEFAULT_WEIGHTS = {"R": 45.0, "P": 30.0, "T": 15.0, "D": 10.0}
SUB_RATIO = {
    "R": {"verifier": 35.0 / 45, "code_quality": 10.0 / 45},
    "P": {"planning": 1 / 3, "discipline": 1 / 3, "self_test": 1 / 3},
}

CSS = """
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:22px;border-bottom:2px solid #d0d7de;padding-bottom:8px}
h2{font-size:17px;margin-top:32px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:12px}
th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#f6f8fa;cursor:pointer;user-select:none;white-space:nowrap}
tr:nth-child(even){background:#fafbfc}
.warn{background:#fff8c5}
.bad{color:#cf222e;font-weight:600}
.na{background:#f6f8fa;color:#656d76;text-align:center}
.alert{background:#ffebe9;border:1px solid #cf222e;color:#82071e;border-radius:8px;padding:10px 14px;margin:12px 0}
.alert code{background:#ffd7d5;padding:0 4px;border-radius:3px}
.alert ul{margin:6px 0 0}
.good{color:#1a7f37;font-weight:600}
.muted{color:#656d76;font-size:12px}
.card{border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-top:16px}
.badge{display:inline-block;border:1px solid #d0d7de;border-radius:12px;padding:2px 10px;margin:2px;font-size:12px}
.badge.ok{background:#dafbe1}.badge.no{background:#ffebe9}.badge.na{background:#f6f8fa}
details{margin-top:8px}summary{cursor:pointer}pre{background:#f6f8fa;padding:10px;border-radius:6px;overflow:auto;font-size:12px;max-height:480px}
.bar{height:14px;background:#0969da;border-radius:3px}
.footer{margin-top:36px;padding-top:10px;border-top:1px solid #d0d7de;color:#656d76;font-size:12px}
.lead{font-size:14px;line-height:1.7;background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:14px 18px;margin-top:12px}
.prose{font-size:14px;line-height:1.8;border:1px solid #d0d7de;border-radius:8px;padding:16px 20px;margin-top:12px}
.prose h3{font-size:15px;margin:14px 0 6px}
.dims{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}
.dim{flex:1;min-width:170px;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px}
.dim h4{margin:0 0 6px;font-size:14px}
.dim .w{float:right;color:#0969da;font-weight:600}
.dim ul{margin:6px 0 0;padding-left:18px;font-size:12px;color:#444}
.rank{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}
.rankcard{flex:1;min-width:180px;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;position:relative}
.rankcard.r1{border-color:#1a7f37;background:#f0fff4}
.rankcard .pos{font-size:12px;color:#656d76}
.rankcard .score{font-size:26px;font-weight:700}
.rankcard .name{font-size:14px;font-weight:600;margin:2px 0}
.stack{display:flex;height:16px;border-radius:4px;overflow:hidden;border:1px solid #d0d7de;margin-top:6px}
.stack span{display:block;height:100%}
.sr{background:#0969da}.sp{background:#8250df}.st{background:#1a7f37}.sd{background:#bf8700}
.legend{font-size:12px;color:#656d76;margin-top:6px}
.legend b{color:#1f2328}
.k{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}
"""

SORT_JS = """
document.querySelectorAll('th[data-sort]').forEach(function(th){
  th.addEventListener('click',function(){
    var table=th.closest('table'),tbody=table.querySelector('tbody');
    var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
    var num=th.dataset.sort==='num';
    var asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
    Array.prototype.sort.call(tbody.querySelectorAll('tr'),function(a,b){
      var x=a.children[idx].dataset.v||a.children[idx].textContent.trim();
      var y=b.children[idx].dataset.v||b.children[idx].textContent.trim();
      var c=num?(parseFloat(x)-parseFloat(y)):x.localeCompare(y);
      return asc?c:-c;
    }).forEach(function(tr){tbody.appendChild(tr);});
  });
});
"""


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def fmt(x, digits=1) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def load_run(run_id: str) -> dict:
    return load_json(RESULTS_RUNS_DIR / f"{run_id}.run.json")


def load_score(run_id: str) -> dict | None:
    p = RESULTS_SCORES_DIR / f"{run_id}.score.json"
    return load_json(p) if p.is_file() else None


def parse_weights(text: str | None) -> dict:
    if not text:
        return dict(DEFAULT_WEIGHTS)
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        key, val = part[0].upper(), float(part[1:])
        if key not in DEFAULT_WEIGHTS:
            raise ValueError(f"非法权重项: {part}（可用 R/P/T/D）")
        out[key] = val
    for k in DEFAULT_WEIGHTS:
        out.setdefault(k, DEFAULT_WEIGHTS[k])
    return out


def minmax_norm(values: list[float], lower_better: bool = True) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [100.0] * len(values)
    out = []
    for v in values:
        score = (hi - v) / (hi - lo) * 100 if lower_better else (v - lo) / (hi - lo) * 100
        out.append(round(score, 2))
    return out


def ratio_norm(values: list[float]) -> list[float]:
    """比值法效率分（越少越好）：score = min(values)/v × 100。

    替代 min-max，避免小样本（尤其 n=2）下必然出现的 0/100 极端化：
    “较费/较慢者”拿到的是相对最省/最快者的效率百分比，单调且无魔数。
    min<=0 时回退 min-max 以规避除零。
    """
    m = min(values)
    if m <= 0:
        return minmax_norm(values)
    return [round(m / v * 100, 2) for v in values]


def _is_verifier_error(verifier: dict) -> bool:
    """验证器是否因环境原因未能执行（命令不存在/无权限等），区别于代码未通过。"""
    if not verifier:
        return False
    if verifier.get("status") == "error" or verifier.get("execution") == "unavailable":
        return True
    log = (verifier.get("log_excerpt") or "").lower()
    return verifier.get("exit_code") in (126, 127) and ("not found" in log or "permission denied" in log)


def _verifier_reason(verifier: dict) -> str:
    """从验证日志抽取环境异常的原因行，用于告警展示。"""
    log = verifier.get("log_excerpt") or ""
    for ln in log.splitlines():
        ll = ln.lower()
        if "not found" in ll or "permission denied" in ll or "cannot execute" in ll:
            return ln.strip()[:160]
    return (log.strip().splitlines() or ["(无日志)"])[-1][:160]


def token_load(run: dict) -> float:
    u = run["usage"]
    return float(u.get("input_tokens", 0) + u.get("cache_read_tokens", 0)
                 + u.get("cache_creation_tokens", 0) + u.get("output_tokens", 0))


def total_input(u: dict) -> int:
    """总输入 = 非缓存输入 + 缓存命中 + 缓存写入（即本轮发送给模型的全部提示 token）。"""
    return (u.get("input_tokens", 0) + u.get("cache_read_tokens", 0)
            + u.get("cache_creation_tokens", 0))


def cache_hit_rate(u: dict) -> float | None:
    """缓存命中率 = 缓存命中 / 总输入；总输入为 0 时返回 None。"""
    ti = total_input(u)
    return (u.get("cache_read_tokens", 0) / ti) if ti > 0 else None


# 各 harness 的缓存统计口径说明
CACHE_METHOD_NOTE = (
    '<details><summary>统计口径说明（输入 / 缓存命中）</summary>'
    '<p class="legend"><b>输入（含缓存）</b> = 非缓存输入 + 缓存命中 + 缓存写入，'
    '即本次任务发送给模型的全部提示 token（多轮对话会重复发送上下文，故会随轮次累加）。'
    '成本按三段分别计价（非缓存输入价 / 缓存命中价 / 输出价），详见各单报告的成本明细。</p>'
    '<p class="legend"><b>缓存命中</b> 的原始字段因 harness 而异，均已归一化为同一口径：'
    '<br>• <b>claude-code</b>（Anthropic 式）：cache_read_input_tokens 直取；'
    '<br>• <b>qoder-cli</b>（OpenAI 式）：prompt_tokens_details.cached_tokens（非缓存输入 = prompt − cached）；'
    '<br>• <b>opencode</b>：tokens.cache.read；'
    '<br>• <b>pi</b>：usage.cacheRead / cacheWrite；'
    '<br>• <b>qwen</b>：usageMetadata.cachedContentTokenCount（非缓存输入 = prompt − cached）；'
    '<br>• <b>codex</b>：cached_input_tokens（input 含 cached，已扣减）。'
    '<br>各家口径经归一化后一致，可横向对比。</p></details>'
)


def compute_rows(run_ids: list[str], weights: dict) -> list[dict]:
    """逐 run 计算维度分；返回带归一化结果的行列表。"""
    rows = []
    for rid in run_ids:
        run = load_run(rid)
        score = load_score(rid)
        rows.append({"run_id": rid, "run": run, "score": score})

    # 同任务组内归一化（跨 agent 对比）
    by_task: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        by_task.setdefault(r["run"]["task_id"], []).append(i)
    for task_id, idxs in by_task.items():
        tokens = [token_load(rows[i]["run"]) for i in idxs]
        durations = [float(rows[i]["run"].get("duration_s") or 0) for i in idxs]
        token_norms = ratio_norm(tokens)
        duration_norms = ratio_norm(durations)
        for k, i in enumerate(idxs):
            rows[i]["token_norm"] = token_norms[k]
            rows[i]["duration_norm"] = duration_norms[k]

    for r in rows:
        run, score = r["run"], r["score"]
        verifier = (score or {}).get("verifier") or {}
        judge = ((score or {}).get("judge") or {})
        js = judge.get("scores") or {}

        v_score = verifier.get("score")
        judge_ok = bool(js) and all(
            js.get(k) is not None for k in ("code_quality", "planning", "discipline", "self_test"))

        ver_error = _is_verifier_error(verifier)
        judge_only = is_judge_only_verifier(verifier)
        # 质量门槛：代码验证通过且正常完成 → T/D 才计分；环境异常 → N/A；确未通过 → 清零
        # judge-only 任务无客观验证器：T/D 是否计分仅看运行是否正常完成
        if judge_only:
            gate = run.get("status") == "completed"
        else:
            gate = (not ver_error) and (verifier.get("passed", 0) > 0) and run.get("status") == "completed"
        if ver_error:
            t_score = None
            d_score = None
        elif gate:
            t_score = r["token_norm"]
            d_score = r["duration_norm"]
        else:
            t_score = 0.0
            d_score = 0.0

        r_part = weights["R"] * SUB_RATIO["R"]["verifier"] * (v_score or 0) / 100
        p_part = 0.0
        if judge_ok:
            r_part += weights["R"] * SUB_RATIO["R"]["code_quality"] * js["code_quality"] / 100
            p_part = weights["P"] * (
                SUB_RATIO["P"]["planning"] * js["planning"]
                + SUB_RATIO["P"]["discipline"] * js["discipline"]
                + SUB_RATIO["P"]["self_test"] * js["self_test"]) / 100

        if not (judge_ok and v_score is not None):
            composite, renorm = None, False
        elif judge_only:
            # judge-only 任务：verifier 不贡献，按可用权重和（cq+P+T+D，默认 65）重归一到 0–100
            cq_w = weights["R"] * SUB_RATIO["R"]["code_quality"]
            cq_contrib = cq_w * js["code_quality"] / 100
            denom = cq_w + weights["P"] + weights["T"] + weights["D"]
            composite = ((cq_contrib + p_part
                          + weights["T"] * (t_score or 0) / 100
                          + weights["D"] * (d_score or 0) / 100) / denom * 100
                         if denom else None)
            renorm = True
        elif ver_error:
            # 验证环境异常：仅用 R 的代码质量子项 + P，按其权重和重归一化到 0–100
            cq_w = weights["R"] * SUB_RATIO["R"]["code_quality"]
            cq_contrib = cq_w * js["code_quality"] / 100
            denom = cq_w + weights["P"]
            composite = (cq_contrib + p_part) / denom * 100 if denom else None
            renorm = True
        else:
            composite = (r_part + p_part
                         + weights["T"] * (t_score or 0) / 100
                         + weights["D"] * (d_score or 0) / 100)
            renorm = False

        r.update({
            "verifier_score": v_score,
            "verifier_error": ver_error,
            "judge_only": judge_only,
            "judge_ok": judge_ok,
            "judge_scores": js,
            "t_score": round(t_score, 2) if t_score is not None else None,
            "d_score": round(d_score, 2) if d_score is not None else None,
            "gated": gate,
            "composite_renormalized": renorm,
            "composite": round(composite, 2) if composite is not None else None,
        })
    return rows


def group_medians(rows: list[dict]) -> list[dict]:
    """按 task×agent+model 分组取中位数（n=1 时自然退化）。"""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["run"]["task_id"], r["run"]["agent"]["harness"], r["run"]["agent"]["model"])
        groups.setdefault(key, []).append(r)
    out = []
    for (task_id, harness, model), members in sorted(groups.items()):
        composites = [m["composite"] for m in members if m["composite"] is not None]
        entry = {
            "task_id": task_id, "harness": harness, "model": model,
            "n": len(members), "members": members,
            "judge_only": any(m.get("judge_only") for m in members),
            "composite_med": round(statistics.median(composites), 2) if composites else None,
            "composite_min": min(composites) if composites else None,
            "composite_max": max(composites) if composites else None,
        }
        for field in ("verifier_score", "t_score", "d_score"):
            vals = [m[field] for m in members
                    if m.get(field) is not None
                    and not (field == "verifier_score"
                             and (m.get("verifier_error") or m.get("judge_only")))]
            entry[field + "_med"] = round(statistics.median(vals), 2) if vals else None
        out.append(entry)
    return out


# ---------------- 渲染 ----------------

def page(title: str, body: str) -> str:
    return (f"<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}"
            f"<script>{SORT_JS}</script></body></html>")


def signal_badges(process: dict | None) -> str:
    if not process:
        return '<span class="muted">过程信号缺失</span>'
    def badge(ok, label):
        cls = "ok" if ok else ("no" if ok is False else "na")
        text = {"ok": "✓", "no": "✗", "na": "-"}[cls]
        return f'<span class="badge {cls}">{text} {esc(label)}</span>'
    p = process
    parts = [badge(p.get("planning"), "规划")]
    if p.get("plan_items"):
        parts.append(f'<span class="badge na">计划 {p.get("plan_completed", 0)}/{p["plan_items"]}</span>')
    sa = p.get("scope_adherence")
    parts.append(f'<span class="badge na">范围符合 {fmt(sa * 100 if sa is not None else None, 0)}%</span>')
    st_label = "末次编辑后自测"
    if p.get("self_test_after_last_edit") and p.get("self_test_kind"):
        st_label += f"({p['self_test_kind']})"
    parts.append(badge(p.get("self_test_after_last_edit"), st_label))
    if p.get("retry_pattern_max"):
        parts.append(f'<span class="badge no">无效重试×{p["retry_pattern_max"]}</span>')
    if p.get("failed_tool_calls"):
        parts.append(f'<span class="badge na">失败调用 {p["failed_tool_calls"]}</span>')
    return " ".join(parts)


def cost_breakdown_html(run: dict) -> str:
    """展开成本计算过程：每段 token × 单价 = 小计，并与 run 记录的 cost_usd 交叉核验。"""
    u = run["usage"]
    model = u.get("cost_model") or run["agent"]["model"]
    bd = pricing.cost_breakdown(
        model,
        u.get("input_tokens", 0),
        u.get("cache_read_tokens", 0),
        u.get("output_tokens", 0),
        u.get("cache_creation_tokens", 0),
    )
    if bd is None:
        return (f'<p class="bad">⚠ 模型 {esc(model)} 不在价格表中，无法展开成本明细</p>')

    rows = []
    for ln in bd["lines"]:
        raw = f"{ln['raw_price']} {ln['raw_currency']}/M"
        if ln["raw_currency"] == "CNY":
            raw += f" ÷ {bd['cny_per_usd']}"
        rows.append(
            f"<tr><td>{esc(ln['label'])}</td>"
            f"<td style='text-align:right'>{ln['tokens']:,}</td>"
            f"<td style='text-align:right'>{esc(raw)}</td>"
            f"<td style='text-align:right'>${ln['usd_per_m']:.4f}/M</td>"
            f"<td style='text-align:right'>${ln['subtotal_usd']:.6f}</td></tr>")

    recorded = u.get("cost_usd", 0)
    drift = abs(recorded - bd["total_usd"])
    if drift <= 1e-6:
        check = f'<span class="good">✓ 与 run 记录 ${recorded:.6f} 一致</span>'
    else:
        check = f'<span class="bad">⚠ 与 run 记录 ${recorded:.6f} 不一致（差 {drift:.6f}）</span>'

    return (
        "<table><tr><th>项目</th><th style='text-align:right'>token</th>"
        "<th style='text-align:right'>原始标价</th>"
        "<th style='text-align:right'>换算单价</th>"
        "<th style='text-align:right'>小计</th></tr>"
        + "".join(rows)
        + f"<tr><td colspan='4' style='text-align:right'><b>合计</b></td>"
          f"<td style='text-align:right'><b>${bd['total_usd']:.6f}</b></td></tr></table>"
        + f'<p class="muted">计费口径：{esc(bd["model_key"])} 官方价 · '
          f'price_version={esc(bd["price_version"])} · 汇率 1 USD = {bd["cny_per_usd"]} CNY · {check}'
        + (f" · Credits 消耗（附注）: {u['credits_consumed']}" if u.get("credits_consumed") is not None else "")
        + "</p>")


def raw_data_html(run_id: str, run: dict, score: dict | None) -> str:
    """原始测试数据（默认收起）：transcript 摘要 / run.json / score.json / 工具调用序列。"""
    parts = ['<details class="card"><summary><b>原始测试数据（点击展开）</b></summary>']
    run_dir = RUNS_DIR / run_id

    summary_path = run_dir / "transcript_summary.txt"
    if summary_path.is_file():
        parts.append("<details><summary>transcript 摘要</summary><pre>"
                     + esc(summary_path.read_text(encoding="utf-8")) + "</pre></details>")

    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        lines = []
        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "tool_call":
                    desc = ev.get("name") or "?"
                    if ev.get("command"):
                        desc += f": {ev['command'][:140]}"
                    elif ev.get("files"):
                        desc += f": {', '.join(ev['files'][:3])}"
                    lines.append(f"[{ev.get('tool_class', '?')}] {desc}")
        if lines:
            parts.append(f"<details><summary>工具调用序列（{len(lines)} 次）</summary><pre>"
                         + esc("\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines)))
                         + "</pre></details>")

    parts.append("<details><summary>run.json（归一化运行记录）</summary><pre>"
                 + esc(json.dumps(run, ensure_ascii=False, indent=2)) + "</pre></details>")
    if score:
        parts.append("<details><summary>score.json（评分记录）</summary><pre>"
                     + esc(json.dumps(score, ensure_ascii=False, indent=2)) + "</pre></details>")
    parts.append("</details>")
    return "".join(parts)


def _single_body(run_id: str, heading_level: str = "h1") -> str:
    """单 agent 报告主体（无 <html> 外壳），供独立报告与对比报告内嵌复用。"""
    run = load_run(run_id)
    score = load_score(run_id)
    verifier = (score or {}).get("verifier") or {}
    judge = (score or {}).get("judge") or {}
    js = judge.get("scores") or {}
    params = run.get("agent", {}).get("params") or {}

    warns = []
    if run.get("model_mismatch"):
        warns.append(f"模型不一致：声明 {run['agent']['model']}，实际 {run.get('actual_model')}")
    if run.get("status") != "completed":
        warns.append(f"运行状态异常：{run.get('status')}")
    if run.get("human_intervention"):
        warns.append(f"人工干预 {run['human_intervention']} 次")

    body = [f"<{heading_level}>单 Agent 运行分析 · {esc(run_id)}</{heading_level}>"]
    for w in warns:
        body.append(f'<p class="warn card">⚠ {esc(w)}</p>')

    body.append("<div class=\"card\"><h3>基本信息</h3><table>"
                f"<tr><th>任务</th><td>{esc(run['task_id'])}</td></tr>"
                f"<tr><th>Agent</th><td>{esc(run['agent']['harness'])} {esc(run['agent'].get('version') or '')}</td></tr>"
                f"<tr><th>模型</th><td>{esc(run['agent']['model'])}（实际：{esc(run.get('actual_model') or '未知')}）</td></tr>"
                f"<tr><th>参数</th><td>上下文 {esc(params.get('context_window'))} · 思考强度 {esc(params.get('thinking_effort'))}</td></tr>"
                f"<tr><th>耗时</th><td>{fmt(run.get('duration_s'))} 秒（{run.get('turns', 0)} 轮 / {run.get('tool_calls', 0)} 次工具调用）</td></tr>"
                f"<tr><th>状态</th><td>{esc(run.get('status'))}</td></tr>"
                "</table></div>")

    u = run["usage"]
    hit = cache_hit_rate(u)
    hit_txt = (fmt(hit * 100, 1) + "%") if hit is not None else "-"
    body.append("<div class=\"card\"><h3>Token 与成本</h3><table>"
                "<tr><th>输入(含缓存)</th><th>缓存命中</th><th>命中率</th><th>输出</th><th>成本(USD)</th></tr>"
                f"<tr><td>{total_input(u):,}</td><td>{u.get('cache_read_tokens', 0):,}</td>"
                f"<td>{hit_txt}</td><td>{u.get('output_tokens', 0):,}</td>"
                f"<td>${u.get('cost_usd', 0):.4f}</td></tr></table>"
                '<p class="legend">输入(含缓存) = 非缓存输入 + 缓存命中 + 缓存写入；'
                '命中率 = 缓存命中 / 输入(含缓存)。三段拆分与计价见下方明细。</p>'
                "<details open><summary>成本计算过程（token × 单价）</summary>"
                + cost_breakdown_html(run)
                + "</details></div>")

    if is_judge_only_verifier(verifier):
        body.append("<div class=\"card\"><h3>客观检查（verifier）</h3>"
                    "<p class=\"muted\">本任务无客观验证器（judge-only 自定义任务）："
                    "以任务提示词的显式要求为验收标准，由 L2 judge 盲评评分。</p></div>")
    elif verifier:
        body.append(f"<div class=\"card\"><h3>客观检查（verifier）</h3>"
                    f"<p>通过 <b>{verifier.get('passed', 0)}</b> / {verifier.get('total', 0)}，"
                    f"得分 <b>{fmt(verifier.get('score'))}</b></p>"
                    f"<details><summary>verifier 日志摘录</summary><pre>{esc(verifier.get('log_excerpt') or '')}</pre></details></div>")

    body.append(f"<div class=\"card\"><h3>过程信号（L1 探测）</h3><p>{signal_badges(run.get('process'))}</p></div>")

    if js:
        flags = js.get("flags") or []
        flag_html = ("".join(f'<span class="badge no">⚑ {esc(f)}</span>' for f in flags)) if flags else ""
        body.append(f"<div class=\"card\"><h3>Judge 盲评（{esc(judge.get('model'))} · rubric {esc(judge.get('rubric_version'))}）</h3>"
                    "<table><tr><th>代码质量</th><th>规划</th><th>执行纪律</th><th>自测</th></tr>"
                    f"<tr><td>{fmt(js.get('code_quality'))}</td><td>{fmt(js.get('planning'))}</td>"
                    f"<td>{fmt(js.get('discipline'))}</td><td>{fmt(js.get('self_test'))}</td></tr></table>"
                    + flag_html
                    + f"<p>{esc(js.get('comments') or '')}</p></div>")
    else:
        body.append('<div class="card"><h3>Judge 盲评</h3><p class="muted">未评分</p></div>')

    body.append(raw_data_html(run_id, run, score))

    if run.get("notes"):
        body.append(f"<p class=\"muted\">备注：{esc(run['notes'])}</p>")
    return "\n".join(body)


def dim_scores(r: dict) -> dict | None:
    """返回单次 run 的四大维度 0-100 分（R 结果 / P 过程 / T token / D 时间）。

    验证环境异常时 T/D 为 None（展示 N/A），R 仅含代码质量（r_cq_only=True）。
    judge-only 任务 R 仅含代码质量（r_judge_only=True），T/D 正常。
    """
    if not r.get("judge_ok"):
        return None
    js = r["judge_scores"]
    pp = (js["planning"] * SUB_RATIO["P"]["planning"]
          + js["discipline"] * SUB_RATIO["P"]["discipline"]
          + js["self_test"] * SUB_RATIO["P"]["self_test"])
    if r.get("verifier_error"):
        rr = js["code_quality"]
        t = d = None
    elif r.get("judge_only"):
        rr = js["code_quality"]
        t = r.get("t_score")
        d = r.get("d_score")
    else:
        rr = ((r.get("verifier_score") or 0) * SUB_RATIO["R"]["verifier"]
              + js["code_quality"] * SUB_RATIO["R"]["code_quality"])
        t = r.get("t_score")
        d = r.get("d_score")
    return {"R": round(rr, 1), "P": round(pp, 1),
            "T": round(t, 1) if t is not None else None,
            "D": round(d, 1) if d is not None else None,
            "r_cq_only": bool(r.get("verifier_error")),
            "r_judge_only": bool(r.get("judge_only"))}


def task_section_html(task_id: str) -> str:
    """测试任务说明：task.yaml 元数据 + prompt.md 原文。"""
    import yaml
    task_dir = TASKS_DIR / task_id
    task_yaml = task_dir / "task.yaml"
    prompt_md = task_dir / "prompt.md"
    if not task_yaml.is_file():
        return ""
    with open(task_yaml, "r", encoding="utf-8") as f:
        task = yaml.safe_load(f) or {}
    prompt = prompt_md.read_text(encoding="utf-8") if prompt_md.is_file() else ""
    return (
        "<h2>测试任务说明</h2>"
        '<div class="card"><table>'
        f"<tr><th>任务</th><td>{esc(task.get('title') or task_id)}（{esc(task_id)}）</td></tr>"
        f"<tr><th>类型 / 难度</th><td>{esc(task.get('category'))} / {esc(task.get('difficulty'))}</td></tr>"
        f"<tr><th>语言</th><td>{esc(task.get('language'))}</td></tr>"
        f"<tr><th>超时</th><td>{esc(task.get('timeout_s'))} 秒</td></tr>"
        f"<tr><th>prompt 版本</th><td>{esc(task.get('prompt_version'))}</td></tr>"
        "</table>"
        f"<details open><summary>任务提示词（发给每个被测 agent 的完整 prompt）</summary>"
        f"<pre>{esc(prompt)}</pre></details></div>")


def env_table_html(rows: list[dict]) -> str:
    """测试环境：每个参评候选的 agent + model + reasoning effort。"""
    trs = []
    for r in rows:
        run = r["run"]
        params = run.get("agent", {}).get("params") or {}
        trs.append(
            f"<tr><td>{esc(r['run_id'])}</td>"
            f"<td>{esc(run['agent']['harness'])} {esc(run['agent'].get('version') or '')}</td>"
            f"<td>{esc(run['agent']['model'])}</td>"
            f"<td>{esc(run.get('actual_model') or '-')}</td>"
            f"<td>{esc(params.get('thinking_effort'))}</td>"
            f"<td>{esc(params.get('context_window'))}</td>"
            f"<td>{esc(run.get('status'))}</td></tr>")
    return ("<h2>测试环境</h2>"
            "<table><thead><tr><th>run</th><th>Agent(harness)</th><th>声明模型</th>"
            "<th>实际模型</th><th>思考强度</th><th>上下文窗口</th><th>状态</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


METRIC_GLOSSARY = [
    ("verifier 得分", "L0 客观检查", "任务自带 pytest 验证脚本的通过率（0–100）。运行期对 agent 不可见，不可作弊。"),
    ("代码质量", "L2 judge 盲评", "最终产物质量：命名/结构/边界处理/修改克制度；含自建测试有效性（rubric v3 维度一）。"),
    ("规划", "L2 judge 盲评（L1 信号为锚）", "动手前是否有覆盖需求、可验证、有序的计划（rubric v3 维度二）。"),
    ("执行纪律", "L2 judge 盲评（L1 信号为锚）", "按计划推进、范围控制、失败后诊断-修复-复验而非盲目重试（rubric v3 维度三）。"),
    ("自测验证", "L2 judge 盲评（L1 信号为锚）", "agent 自己的验证行为：末次编辑后是否测试、TDD/分层测试证据（rubric v3 维度四）。"),
    ("输入(含缓存)", "运行日志", "发送给模型的全部提示 token = 非缓存输入 + 缓存命中 + 缓存写入。"),
    ("缓存命中 / 命中率", "运行日志", "命中提示缓存的 token 数及其占总输入比例；命中率高说明 harness 上下文管理高效。"),
    ("输出", "运行日志", "模型生成的 token（含思考/推理 token）。"),
    ("成本(USD)", "价格表折算", "三段 token × 模型官方单价，与 agent 自有计费解耦，保证横向可比。"),
    ("耗时", "运行日志", "首末事件时间差（秒）。"),
    ("T token 成本分", "组内归一化", "同任务同批次内 min-max 归一（0–100，越省越高）；质量门槛不过则记 0。"),
    ("D 时间成本分", "组内归一化", "同上，按耗时归一。"),
    ("L1 过程信号", "脚本探测", "从日志客观提取：是否规划 / 计划完成度 / 范围符合率 / 末编辑后自测 / 无效重试 / 失败调用。"),
]


def methodology_html(weights: dict) -> str:
    """评估方法 + 测评指标词汇表。"""
    r_v = weights["R"] * SUB_RATIO["R"]["verifier"]
    r_q = weights["R"] * SUB_RATIO["R"]["code_quality"]
    p_each = weights["P"] / 3
    glossary_rows = "".join(
        f"<tr><td><b>{esc(m)}</b></td><td>{esc(src)}</td><td>{esc(desc)}</td></tr>"
        for m, src, desc in METRIC_GLOSSARY)
    return (
        "<h2>评估方法与指标含义</h2>"
        '<p class="lead">本报告对每个「任务 × agent+模型」从四个维度打分：'
        '<b>R 任务结果</b>（做对了没）、<b>P harness 过程能力</b>（怎么做的）、'
        '<b>T token 成本</b>（多省）、<b>D 时间成本</b>（多快）。分数来自三层取证，逐层降低主观性：<br>'
        '① <b>L0 客观检查</b>：任务自带 verifier（pytest 等确定性脚本），0 成本、不可作弊；'
        '② <b>L1 过程探测</b>：脚本从运行日志提取客观行为信号（是否规划/范围符合/末编辑后自测/无效重试）；'
        '③ <b>L2 Judge 盲评</b>：固定 rubric v3、不告知产出方身份，只评客观检查覆盖不到的质量面。</p>'
        '<div class="dims">'
        f'<div class="dim"><span class="w">{weights["R"]:.0f}分</span><h4>R 任务结果</h4>'
        f'<ul><li>verifier 通过率 {r_v:.0f}（L0）</li><li>代码质量 {r_q:.0f}（L2 judge）</li></ul></div>'
        f'<div class="dim"><span class="w">{weights["P"]:.0f}分</span><h4>P 过程能力</h4>'
        f'<ul><li>规划 {p_each:.0f}</li><li>执行纪律 {p_each:.0f}</li><li>自测验证 {p_each:.0f}</li>'
        '<li class="muted">L1 信号为锚，L2 judge 评质量</li></ul></div>'
        f'<div class="dim"><span class="w">{weights["T"]:.0f}分</span><h4>T token 成本</h4>'
        '<ul><li>总 token 组内归一</li><li class="muted">越省越高</li></ul></div>'
        f'<div class="dim"><span class="w">{weights["D"]:.0f}分</span><h4>D 时间成本</h4>'
        '<ul><li>耗时组内归一</li><li class="muted">越快越高</li></ul></div>'
        '</div>'
        '<p class="legend">'
        f'<b>综合分</b> = R×{weights["R"]/100:.2f} + P×{weights["P"]/100:.2f} + '
        f'T×{weights["T"]/100:.2f} + D×{weights["D"]/100:.2f}（各维度先归一到 0–100 再加权）。'
        '关键规则：<b>组内归一化</b>—成本不看绝对值，而是同任务同批 agent 间 min-max 排名；'
        '<b>质量门槛</b>—verifier 全挂或超时/报错的 run，T/D 记 0（防止“快速失败”拿效率分）；'
        '<b>成本口径</b>—三段 token×模型官方价，与 agent 自有计费解耦；n>1 时取中位数。</p>'
        "<details><summary>测评指标词汇表（各指标含义与证据来源）</summary>"
        "<table><thead><tr><th>指标</th><th>证据来源</th><th>含义</th></tr></thead>"
        f"<tbody>{glossary_rows}</tbody></table></details>")


def analysis_html(run_ids: list[str], rows: list[dict]) -> str:
    """对比分析正文（LLM 撰写，results/analysis/ 缓存）。缺失时返回占位提示。"""
    try:
        from judge import compare_key, ANALYSIS_DIR
    except ImportError:
        return ""
    key = compare_key(run_ids)
    path = ANALYSIS_DIR / f"{key}.json"
    if not path.is_file():
        return ('<h2>对比分析</h2><p class="muted">尚未生成 LLM 对比分析正文。'
                '运行 <code>python scripts/judge.py compare &lt;run_ids...&gt;</code> 后重新生成报告。</p>')
    data = load_json(path)
    by_id = {r["run_id"]: r for r in rows}
    legend_items = []
    for label, rid in (data.get("labels") or {}).items():
        r = by_id.get(rid)
        if r:
            agent = r["run"]["agent"]
            legend_items.append(f"<b>{esc(label)}</b> = {esc(agent['harness'])} / {esc(agent['model'])}")
        else:
            legend_items.append(f"<b>{esc(label)}</b> = {esc(rid)}")
    md = data.get("analysis_md") or ""
    # 轻量 markdown → HTML（### 标题 / 段落 / 加粗）
    html_parts = []
    for block in md.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("### "):
            html_parts.append(f"<h3>{esc(block[4:])}</h3>")
        elif block.startswith("## "):
            html_parts.append(f"<h3>{esc(block[3:])}</h3>")
        else:
            text = esc(block)
            import re as _re
            text = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = text.replace("\n", "<br>")
            html_parts.append(f"<p>{text}</p>")
    return (
        "<h2>对比分析（LLM 撰写）</h2>"
        f'<p class="legend">盲评标签对照：{"；".join(legend_items)}。'
        f'分析由 judge 模型 {esc(data.get("judge_model"))} 基于匿名数据撰写。</p>'
        f'<div class="prose">{"".join(html_parts)}</div>')


def ranking_html(groups: list[dict]) -> str:
    """排名卡片 + 数据驱动的差异来源结论。"""
    ranked = sorted(
        [g for g in groups if g["composite_med"] is not None],
        key=lambda g: g["composite_med"], reverse=True)
    if not ranked:
        return ""
    cards = []
    for i, g in enumerate(ranked):
        cls = " r1" if i == 0 else ""
        cards.append(
            f'<div class="rankcard{cls}"><div class="pos">#{i + 1}</div>'
            f'<div class="score">{fmt(g["composite_med"])}</div>'
            f'<div class="name">{esc(g["harness"])}</div>'
            f'<div class="muted">{esc(g["model"])}</div></div>')
    # 差异来源：看哪个维度组间极差最大
    def spread(key):
        vals = [g[key] for g in ranked if g.get(key) is not None]
        return (max(vals) - min(vals)) if len(vals) >= 2 else 0
    all_judge_only = all(g.get("judge_only") for g in ranked)
    spans = {"token 成本": spread("t_score_med"),
             "时间成本": spread("d_score_med")}
    if not all_judge_only:
        spans = {"任务结果": spread("verifier_score_med"), **spans}
    driver = max(spans, key=spans.get) if spans else ""
    verifier_vals = [g["verifier_score_med"] for g in ranked if g.get("verifier_score_med") is not None]
    same_result = verifier_vals and (max(verifier_vals) - min(verifier_vals) < 1e-6)
    if all_judge_only:
        result_clause = "纯 judge 评分任务（无客观验证器），排名由 judge 盲评与过程/效率维度拉开；"
    elif same_result:
        result_clause = "任务结果（verifier）均为满分、不分伯仲；"
    else:
        result_clause = "各对象任务结果存在差异；"
    lead = (
        f'<p class="legend">共 {len(ranked)} 个参评对象。'
        + result_clause
        + f'拉开排名的主要维度是【{driver}】（组间极差最大）。'
        + '详见下方“评分构成拆解”与“总览”。</p>')
    return "<h2>排名与结论</h2>" + '<div class="rank">' + "".join(cards) + "</div>" + lead


def breakdown_html(rows: list[dict], weights: dict) -> str:
    """评分构成拆解表：每个 run 的 R/P/T/D 维度分（0-100）+ 加权贡献 + 堆叠条。"""
    trs = []
    for r in rows:
        d = dim_scores(r)
        if d is None:
            trs.append(f'<tr><td>{esc(r["run_id"])}</td><td colspan="7" class="muted">未完成评分</td></tr>')
            continue
        run = r["run"]
        comp = r.get("composite")
        if r.get("judge_only"):
            comp_txt = fmt(comp) + "‡"
        elif r.get("composite_renormalized"):
            comp_txt = fmt(comp) + "†"
        else:
            comp_txt = fmt(comp)
        # judge-only 任务的 R 仅含代码质量，有效权重为 R×code_quality 子比例
        r_weight = (weights["R"] * SUB_RATIO["R"]["code_quality"]
                    if d.get("r_judge_only") else weights["R"])
        wr = (d["R"] or 0) * (r_weight / 100)
        wp = (d["P"] or 0) * (weights["P"] / 100)
        wt = (d["T"] or 0) * (weights["T"] / 100)
        wd = (d["D"] or 0) * (weights["D"] / 100)
        stack = (f'<div class="stack" title="R {wr:.1f} / P {wp:.1f} / T {wt:.1f} / D {wd:.1f}">'
                 f'<span class="sr" style="width:{wr}%"></span>'
                 f'<span class="sp" style="width:{wp}%"></span>'
                 f'<span class="st" style="width:{wt}%"></span>'
                 f'<span class="sd" style="width:{wd}%"></span></div>')
        p_cell = f'<td data-v="{d["P"]}">{fmt(d["P"])} <span class="muted">(+{wp:.1f})</span></td>'
        t_cell = ('<td class="na">N/A</td>' if d["T"] is None
                  else f'<td data-v="{d["T"]}">{fmt(d["T"])} <span class="muted">(+{wt:.1f})</span></td>')
        d_cell = ('<td class="na">N/A</td>' if d["D"] is None
                  else f'<td data-v="{d["D"]}">{fmt(d["D"])} <span class="muted">(+{wd:.1f})</span></td>')
        r_star = "*" if d.get("r_cq_only") else ("‡" if d.get("r_judge_only") else "")
        row_cls = ' class="warn"' if r.get("verifier_error") else ""
        trs.append(
            f'<tr{row_cls}><td>{esc(run["agent"]["harness"])}<br><span class="muted">{esc(run["agent"]["model"])}</span></td>'
            f'<td data-v="{comp if comp is not None else -1}"><b>{comp_txt}</b>{stack}</td>'
            f'<td data-v="{d["R"]}">{fmt(d["R"])}{r_star} <span class="muted">(+{wr:.1f})</span></td>'
            f'{p_cell}{t_cell}{d_cell}</tr>')
    legend = ('<p class="legend"><span class="k sr"></span>R 任务结果'
              '<span class="k sp" style="margin-left:12px"></span>P 过程能力'
              '<span class="k st" style="margin-left:12px"></span>T token 成本'
              '<span class="k sd" style="margin-left:12px"></span>D 时间成本'
              '。括号内为该维度对综合分的加权贡献，堆叠条宽度与之成比。'
              '<b>N/A</b>=该维度因验证环境异常不可信；<b>*</b>=R 仅含代码质量（verifier 缺失）；'
              '<b>†</b>=综合分已按可用维度权重（代码质量+过程能力）重归一化，不可与正常运行直接比；'
              '<b>‡</b>=judge-only 任务（无客观验证器），R 仅含代码质量，综合分按 cq+P+T+D '
              '权重和（默认 65）归一化，不与带 verifier 的任务混比。</p>')
    return ("<h2>评分构成拆解（维度分 0–100 · 括号为加权贡献）</h2>"
            "<table><thead><tr><th>Agent</th><th data-sort=\"num\">综合分</th>"
            "<th data-sort=\"num\">R 任务结果</th><th data-sort=\"num\">P 过程能力</th>"
            "<th data-sort=\"num\">T token 成本</th><th data-sort=\"num\">D 时间成本</th></tr></thead><tbody>"
            + "".join(trs) + "</tbody></table>" + legend)


def render_single(run_id: str, out_dir: Path) -> Path:
    body = _single_body(run_id, heading_level="h1")
    body += f'<div class="footer">生成时间 {datetime.now().isoformat(timespec="seconds")} · ide-benchmark</div>'
    out_path = out_dir / f"report-{run_id}.html"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page(f"run {run_id}", body), encoding="utf-8")
    return out_path


def render_compare(run_ids: list[str], weights: dict, out_dir: Path) -> Path:
    rows = compute_rows(run_ids, weights)
    groups = group_medians(rows)

    body = ["<h1>AI 编码工具对比测评报告</h1>",
            f"<p class=\"muted\">参评对象 {len(rows)} 次运行 · 归为 {len(groups)} 个「任务×agent+模型」分组 · "
            f"权重 R={weights['R']:.0f}/P={weights['P']:.0f}/T={weights['T']:.0f}/D={weights['D']:.0f}</p>"]

    err_rows = [r for r in rows if r.get("verifier_error")]
    if err_rows:
        items = "".join(
            f"<li><code>{esc(r['run_id'])}</code>：{esc(_verifier_reason((r.get('score') or {}).get('verifier') or {}))}</li>"
            for r in err_rows)
        body.append('<div class="alert"><b>⚠ 验证环境异常</b>：以下运行的客观验证器未能执行'
                    '（如 <code>python</code> 命令不存在），其 T / D 与 verifier 不可信，综合分已按'
                    '「代码质量 + 过程能力」重归一化并标 †，<b>不可与正常运行直接比较</b>。<ul>'
                    + items + '</ul></div>')

    if any(r.get("judge_only") for r in rows):
        body.append('<p class="legend"><b>注</b>：本报告含 judge-only 任务（Web 创建的纯提示词自定义任务）：'
                    '无 L0 客观分，综合分按「代码质量 + 过程能力 + 效率」权重和（默认 65）归一化并标 ‡，'
                    '<b>不应与带 verifier 的任务混比</b>（向导按单任务出报告，天然不混）。</p>')

    # 任务说明 + 测试环境 + 评估方法 + 排名 + LLM 对比分析 + 评分构成拆解
    task_ids = sorted({r["run"]["task_id"] for r in rows})
    for tid in task_ids:
        body.append(task_section_html(tid))
    body.append(env_table_html(rows))
    body.append(methodology_html(weights))
    body.append(ranking_html(groups))
    body.append(analysis_html(run_ids, rows))
    body.append(breakdown_html(rows, weights))

    # 总览表（原始指标 + 归一化分）
    body.append('<h2>总览：原始指标与归一化分</h2>')
    body.append('<p class="legend">前段为得分（综合分/任务结果/token 成本分/时间成本分，均为 0–100），'
                '后段为可核验的原始指标（输入含缓存/缓存命中/命中率/输出/成本/耗时）。表头可点击排序；'
                '“区间”仅 n>1 时展示（稳定性参考）；高亮行表示模型不一致/异常状态/效率清零。</p>')
    body.append(CACHE_METHOD_NOTE)

    head = ("<tr><th data-sort=\"text\">任务</th><th data-sort=\"text\">Agent</th>"
            "<th data-sort=\"text\">模型</th><th data-sort=\"num\">n</th>"
            "<th data-sort=\"num\">综合分(中位)</th><th data-sort=\"num\">区间</th>"
            "<th data-sort=\"num\">任务结果</th><th data-sort=\"num\">token 成本分</th><th data-sort=\"num\">时间成本分</th>"
            "<th data-sort=\"num\">输入(含缓存)</th><th data-sort=\"num\">缓存命中</th>"
            "<th data-sort=\"num\">命中率</th><th data-sort=\"num\">输出</th>"
            "<th data-sort=\"num\">成本</th><th data-sort=\"num\">耗时(s)</th><th>标记</th></tr>")
    trs = []
    for g in groups:
        med = g["composite_med"]
        bar = f'<div class="bar" style="width:{med or 0:.0f}px"></div>' if med is not None else ""
        comp_cell = f'{fmt(med)} {bar}' if med is not None else "-"
        span = ""
        if g["composite_min"] is not None and g["n"] > 1:
            span = f'{fmt(g["composite_min"])}–{fmt(g["composite_max"])}'
        run0 = g["members"][0]
        marks = []
        for m in g["members"]:
            if m["run"].get("model_mismatch"):
                marks.append("模型不一致")
            if m["run"].get("status") != "completed":
                marks.append(esc(m["run"].get("status")))
            if m["run"].get("human_intervention"):
                marks.append(f"干预×{m['run']['human_intervention']}")
            if m.get("verifier_error"):
                marks.append("验证环境异常")
            elif not m.get("gated"):
                marks.append("效率清零")
            if m.get("judge_only") and "纯judge评分" not in marks:
                marks.append("纯judge评分")
        u = run0["run"]["usage"]
        row_cls = ' class="warn"' if any(("不一致" in str(m)) or ("清零" in str(m)) or ("异常" in str(m)) for m in marks) else ""
        trs.append(
            f"<tr{row_cls}><td>{esc(g['task_id'])}</td><td>{esc(g['harness'])}</td><td>{esc(g['model'])}</td>"
            f"<td data-v=\"{g['n']}\">{g['n']}</td>"
            f"<td data-v=\"{med if med is not None else -1}\">{comp_cell}</td>"
            f"<td>{span}</td>"
            + (f'<td data-v="-1">—<span class="muted">（纯judge）</span></td>'
               if g.get("judge_only") else
               f"<td data-v=\"{g['verifier_score_med'] if g['verifier_score_med'] is not None else -1}\">{fmt(g['verifier_score_med'])}</td>") +
            f"<td data-v=\"{g['t_score_med'] if g['t_score_med'] is not None else -1}\">{fmt(g['t_score_med'])}</td>"
            f"<td data-v=\"{g['d_score_med'] if g['d_score_med'] is not None else -1}\">{fmt(g['d_score_med'])}</td>"
            f'<td data-v="{total_input(u)}">{total_input(u):,}</td>'
            f'<td data-v="{u.get("cache_read_tokens", 0)}">{u.get("cache_read_tokens", 0):,}</td>'
            f'<td data-v="{(cache_hit_rate(u) or 0)}">{(fmt(cache_hit_rate(u) * 100, 1) + "%") if cache_hit_rate(u) is not None else "-"}</td>'
            f'<td data-v="{u.get("output_tokens", 0)}">{u.get("output_tokens", 0):,}</td>'
            f'<td data-v="{u.get("cost_usd", 0)}">${u.get("cost_usd", 0):.4f}</td>'
            f'<td data-v="{run0["run"].get("duration_s") or 0}">{fmt(run0["run"].get("duration_s"))}</td>'
            f"<td>{'；'.join(marks)}</td></tr>")
    body.append(f"<table><thead>{head}</thead><tbody>{''.join(trs)}</tbody></table>")

    # 单次运行明细
    body.append("<h2>单次运行明细</h2>")
    for r in rows:
        run, score = r["run"], r["score"]
        verifier = (score or {}).get("verifier") or {}
        js = r.get("judge_scores") or {}
        comp = fmt(r.get("composite")) + ("‡" if r.get("judge_only")
                                          else "†" if r.get("composite_renormalized") else "")
        ver_txt = ("环境异常 N/A" if r.get("verifier_error")
                   else "纯judge（无验证器）" if r.get("judge_only")
                   else f"{verifier.get('passed', '-')}/{verifier.get('total', '-')}")
        result_txt = ("N/A" if r.get("verifier_error")
                      else "—" if r.get("judge_only")
                      else fmt(r.get("verifier_score")))
        body.append(
            f"<div class=\"card\"><b>{esc(r['run_id'])}</b> · 综合分 <b>{comp}</b>"
            f"（结果 {result_txt} · token {fmt(r.get('t_score'))} · 时间 {fmt(r.get('d_score'))}）<br>"
            f"{signal_badges(run.get('process'))}"
            f"<p class=\"muted\">verifier {ver_txt}"
            f" · judge {'已评' if r['judge_ok'] else '未评'}"
            + (f" · {esc(js.get('comments'))}" if js.get("comments") else "")
            + "</p></div>")

    # 附加信息：尾部内嵌完整单 agent 报告（默认折叠，含原始数据）
    body.append("<h2>附：单 Agent 完整报告与原始数据（内嵌，默认收起）</h2>")
    for r in rows:
        comp = fmt(r.get("composite"))
        summary = (f"{esc(r['run_id'])} · 综合分 {comp}"
                   f"（结果 {'—' if r.get('judge_only') else fmt(r.get('verifier_score'))} · "
                   f"token {fmt(r.get('t_score'))} · 时间 {fmt(r.get('d_score'))}）")
        body.append(
            f"<details class=\"card\"><summary><b>{summary}</b></summary>"
            + _single_body(r["run_id"], heading_level="h3")
            + "</details>")

    price_versions = sorted({r["run"]["usage"].get("price_version", "?") for r in rows})
    rubric_versions = sorted({((r["score"] or {}).get("judge") or {}).get("rubric_version", "?") for r in rows})
    judge_tokens = sum(
        (((r["score"] or {}).get("judge") or {}).get("judge_usage") or {}).get("input_tokens", 0)
        + (((r["score"] or {}).get("judge") or {}).get("judge_usage") or {}).get("output_tokens", 0)
        for r in rows)
    body.append(f'<div class="footer">生成时间 {datetime.now().isoformat(timespec="seconds")} · '
                f'价格表版本 {esc("/".join(price_versions))} · rubric 版本 {esc("/".join(rubric_versions))} · '
                f'judge 消耗 {judge_tokens:,} tokens · ide-benchmark</div>')

    ts = datetime.now().strftime("%Y%m%d-%H%M")
    out_path = out_dir / f"report-compare-{ts}.html"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page("ide-benchmark 对比报告", "\n".join(body)), encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 bench HTML 报告")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_single = sub.add_parser("single", help="单 agent 分析报告")
    p_single.add_argument("run_id")
    p_single.add_argument("-o", "--out", default=str(REPORTS_DIR))
    p_cmp = sub.add_parser("compare", help="多 run 对比报告")
    p_cmp.add_argument("run_ids", nargs="+")
    p_cmp.add_argument("--weights", help="如 R45,P30,T15,D10")
    p_cmp.add_argument("-o", "--out", default=str(REPORTS_DIR))
    args = ap.parse_args()

    try:
        if args.cmd == "single":
            out = render_single(args.run_id, Path(args.out))
        else:
            weights = parse_weights(args.weights)
            out = render_compare(args.run_ids, weights, Path(args.out))
    except (FileNotFoundError, ValueError) as exc:
        eprint(f"report 失败: {exc}")
        return 1
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
