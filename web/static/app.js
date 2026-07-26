/* ide-benchmark 五步向导 */
"use strict";

const S = {
  step: 1,
  taskId: null,
  candidates: [],          // {agent, model, context_window, thinking}
  runs: [],                // prepare 返回 [{run_id, work_dir, prompt_text, launch, ...}]
  jobId: null,
};
let AGENTS = [];
let TASKS = [];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function saveState() {
  localStorage.setItem("ide-bench-wizard", JSON.stringify(S));
}
function loadState() {
  try {
    const raw = localStorage.getItem("ide-bench-wizard");
    if (raw) Object.assign(S, JSON.parse(raw));
  } catch (e) { /* 忽略损坏状态 */ }
}

async function api(path, body) {
  const opt = body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : {};
  const resp = await fetch(path, opt);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

function esc(x) {
  const d = document.createElement("div");
  d.textContent = x == null ? "" : String(x);
  return d.innerHTML;
}

/* 复制文本：navigator.clipboard 仅在 HTTPS/localhost 可用（安全上下文），
   经局域网 IP 以 http 访问时为 undefined，需 textarea + execCommand 兜底。 */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) { /* 落入兜底 */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

function bindCopy(btn, getText, label) {
  btn.onclick = async () => {
    const ok = await copyText(getText());
    btn.textContent = ok ? "✓ 已复制" : "✗ 复制失败，请手动选取";
    btn.classList.toggle("copied", ok);
    setTimeout(() => { btn.textContent = label; btn.classList.remove("copied"); }, 1600);
  };
}

/* ---------- 步骤切换 ---------- */
function showStep(n) {
  S.step = n;
  $$("#steps .step").forEach((el) => {
    const k = Number(el.dataset.step);
    el.classList.toggle("active", k === n);
    el.classList.toggle("done", k < n);
  });
  $$("main section").forEach((el) => { el.hidden = Number(el.dataset.panel) !== n; });
  $("#btn-prev").disabled = n <= 1;
  $("#btn-next").disabled = !canAdvance(n);
  $("#btn-next").textContent = n >= 5 ? "完成" : "下一步";
  saveState();
}

function canAdvance(n) {
  if (n === 1) return !!S.taskId;
  if (n === 2) return S.runs.some((r) => r.ok);
  if (n === 3) return true;
  if (n === 4) return S.runs.some((r) => r.collected);
  return false;
}

/* ---------- 步骤 1：任务选择 ---------- */
function renderTasks() {
  const groups = { easy: [], medium: [], hard: [] };
  TASKS.forEach((t) => (groups[t.difficulty] || (groups[t.difficulty] = [])).push(t));
  const box = $("#task-groups");
  box.innerHTML = "";
  const descs = {
    easy: "小型修复 · 约 10 分钟量级",
    medium: "功能实现 · 期望有计划与自建测试",
    hard: "SPEC 驱动多文件应用 · 考察完整工程能力",
  };
  for (const [diff, list] of Object.entries(groups)) {
    if (!list.length) continue;
    const g = document.createElement("div");
    g.className = "task-group";
    g.innerHTML = `<h3>${esc(diff)}<span class="tag ${esc(diff)}">${esc(descs[diff] || "")}</span></h3>`;
    const cards = document.createElement("div");
    cards.className = "task-cards";
    list.forEach((t) => {
      const c = document.createElement("div");
      c.className = "task-card" + (S.taskId === t.task_id ? " selected" : "");
      c.innerHTML = `<div class="tid">${esc(t.title)}</div>
        <div class="meta">${esc(t.task_id)} · ${esc(t.category)} · ${esc(t.language)} · 超时 ${esc(t.timeout_s)}s</div>`;
      c.onclick = () => { S.taskId = t.task_id; renderTasks(); showStep(1); };
      cards.appendChild(c);
    });
    g.appendChild(cards);
    box.appendChild(g);
  }
}

/* ---------- 步骤 2：候选登记与 prepare ---------- */
function candRow(cand = {}) {
  const tr = document.createElement("tr");
  const agentOpts = AGENTS.map((a) => {
    const dis = a.installed ? "" : " (未安装)";
    return `<option value="${esc(a.agent)}" ${cand.agent === a.agent ? "selected" : ""}>${esc(a.agent)}${dis}</option>`;
  }).join("");
  tr.innerHTML = `
    <td><select class="c-agent">${agentOpts}</select></td>
    <td><input class="c-model" placeholder="如 deepseek-v4-pro" value="${esc(cand.model || "")}"></td>
    <td><input class="c-cw" value="${esc(cand.context_window || "1M")}"></td>
    <td><select class="c-th">
      ${["max", "xhigh", "high", "medium", "low", "default"].map((t) =>
        `<option ${cand.thinking === t ? "selected" : ""}>${t}</option>`).join("")}
    </select></td>
    <td><button class="ghost c-del">删除</button></td>`;
  tr.querySelector(".c-del").onclick = () => tr.remove();
  return tr;
}

function readCandidates() {
  return $$("#cand-table tbody tr").map((tr) => ({
    agent: tr.querySelector(".c-agent").value,
    model: tr.querySelector(".c-model").value.trim(),
    context_window: tr.querySelector(".c-cw").value.trim(),
    thinking: tr.querySelector(".c-th").value,
  })).filter((c) => c.model);
}

function renderPrepareResults() {
  const box = $("#prepare-results");
  box.innerHTML = "";
  S.runs.forEach((r) => {
    const card = document.createElement("div");
    card.className = "card";
    if (!r.ok) {
      card.innerHTML = `<h4 class="err">✗ ${esc(r.agent)} / ${esc(r.model)}</h4>
        <p class="err">${esc(r.error)}</p>`;
    } else {
      card.innerHTML = `<h4 class="ok">✓ ${esc(r.run_id)}</h4>
        <p class="muted">工作目录：<code class="path">${esc(r.work_dir)}</code>
          <button class="copy-btn inline wd-copy">复制路径</button></p>
        <div class="copy-wrap">
          <button class="copy-btn prompt-copy">复制提示词</button>
          <pre>${esc(r.prompt_text)}</pre>
        </div>`;
      bindCopy(card.querySelector(".wd-copy"), () => r.work_dir, "复制路径");
      bindCopy(card.querySelector(".prompt-copy"), () => r.prompt_text, "复制提示词");
    }
    box.appendChild(card);
  });
}

async function doPrepare() {
  S.candidates = readCandidates();
  if (!S.candidates.length) { alert("请至少登记一个候选（agent + 模型名）"); return; }
  $("#btn-prepare").disabled = true;
  try {
    S.runs = await api("/api/prepare", { task_id: S.taskId, candidates: S.candidates });
    renderPrepareResults();
    renderLaunchGuides();
    showStep(2);
  } catch (e) {
    alert("生成失败：" + e.message);
  } finally {
    $("#btn-prepare").disabled = false;
  }
}

/* ---------- 步骤 3：运行指引 ---------- */
function renderLaunchGuides() {
  const box = $("#launch-guides");
  box.innerHTML = "";
  S.runs.filter((r) => r.ok).forEach((r) => {
    const info = AGENTS.find((a) => a.agent === r.agent) || {};
    const warn = (info.warnings || []).map((w) => `<p class="warn-note">⚠ ${esc(w)}</p>`).join("");
    const cdCmd = `cd "${r.work_dir}"`;
    const launchCmd = (r.launch && r.launch.command) || r.agent;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<h4>${esc(r.agent)} · ${esc(r.model)} <span class="muted">(${esc(r.run_id)})</span></h4>
      ${warn}
      <p class="muted">${esc((r.launch && r.launch.note) || "")}</p>
      <div class="launch-step"><span class="ln">①</span> 进入工作目录：
        <div class="copy-wrap">
          <button class="copy-btn s1">复制</button>
          <pre>${esc(cdCmd)}</pre>
        </div></div>
      <div class="launch-step"><span class="ln">②</span> 启动编码工具（bypass/yolo 模式），并设置模型 <b>${esc(r.model)}</b>、思考强度 <b>${esc(r.params && r.params.thinking_effort)}</b>：
        <div class="copy-wrap">
          <button class="copy-btn s2">复制</button>
          <pre>${esc(launchCmd)}</pre>
        </div></div>
      <div class="launch-step"><span class="ln">③</span> 粘贴任务提示词开始任务：
        <div class="copy-wrap">
          <button class="copy-btn s3">复制提示词</button>
          <pre class="clamp">${esc(r.prompt_text || "")}</pre>
        </div></div>`;
    bindCopy(card.querySelector(".s1"), () => cdCmd, "复制");
    bindCopy(card.querySelector(".s2"), () => launchCmd, "复制");
    bindCopy(card.querySelector(".s3"), () => r.prompt_text || "", "复制提示词");
    box.appendChild(card);
  });
}

/* ---------- 步骤 4：采集 ---------- */
async function doCollect() {
  const runIds = S.runs.filter((r) => r.ok).map((r) => r.run_id);
  if (!runIds.length) { alert("没有可采集的 run"); return; }
  const btn = $("#btn-collect");
  btn.disabled = true;
  $("#collect-results").innerHTML = '<p><span class="spinner"></span>采集中（定位日志 → 归一化 → verifier → 过程信号）…</p>';
  try {
    const results = await api("/api/collect", { run_ids: runIds });
    const box = $("#collect-results");
    box.innerHTML = "";
    results.forEach((res) => {
      const run = S.runs.find((r) => r.run_id === res.run_id);
      if (run) run.collected = res.ok;
      const card = document.createElement("div");
      card.className = "card";
      if (res.ok) {
        card.innerHTML = `<h4 class="ok">✓ ${esc(res.run_id)}</h4>
          <p>tokens 共 <b>${Number(res.tokens).toLocaleString()}</b> · 成本 <b>$${(res.cost_usd || 0).toFixed(4)}</b>
          · 耗时 <b>${esc(res.duration_s)}</b>s · verifier <b>${esc(res.verifier.passed)}/${esc(res.verifier.total)}</b></p>`;
      } else {
        card.innerHTML = `<h4 class="err">✗ ${esc(res.run_id)}（${esc(res.stage_failed)} 阶段失败）</h4>
          <p class="err">${esc(res.error)}</p>
          <p class="muted">若为「token 全 0」或找不到日志：该 run 作废，请回第三步按指引重跑该工具后再次采集。</p>`;
      }
      box.appendChild(card);
    });
    saveState();
    showStep(4);
  } catch (e) {
    $("#collect-results").innerHTML = `<p class="err">采集失败：${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 步骤 5：报告 ---------- */
async function doReport() {
  const runIds = S.runs.filter((r) => r.ok && r.collected).map((r) => r.run_id);
  if (runIds.length < 2) { alert("至少需要 2 个采集成功的 run 才能对比"); return; }
  const btn = $("#btn-report");
  btn.disabled = true;
  try {
    const { job_id } = await api("/api/report", { run_ids: runIds, weights: $("#weights").value.trim() });
    S.jobId = job_id;
    saveState();
    pollReport();
  } catch (e) {
    $("#report-status").innerHTML = `<p class="err">${esc(e.message)}</p>`;
    btn.disabled = false;
  }
}

async function pollReport() {
  const box = $("#report-status");
  if (!S.jobId) return;
  try {
    const st = await api(`/api/report/status/${S.jobId}`);
    if (st.state === "running") {
      box.innerHTML = `<p><span class="spinner"></span>${esc(st.progress || "处理中")}…</p>`;
      setTimeout(pollReport, 1500);
    } else if (st.state === "done") {
      box.innerHTML = `<p class="ok">✓ 报告已生成</p>
        <a class="report-link" href="${esc(st.report_url)}" target="_blank">打开对比测评报告 →</a>`;
      $("#btn-report").disabled = false;
    } else {
      box.innerHTML = `<p class="err">生成失败：${esc(st.error)}</p>
        <p class="muted">若为 judge 配置缺失：请复制 config/judge.example.json 为 config/judge.json 并填写 API 信息后重试。</p>`;
      $("#btn-report").disabled = false;
    }
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
    $("#btn-report").disabled = false;
  }
}

/* ---------- 初始化 ---------- */
async function init() {
  loadState();
  [TASKS, AGENTS] = await Promise.all([api("/api/tasks"), api("/api/agents")]);
  renderTasks();
  const tbody = $("#cand-table tbody");
  if (S.candidates.length) {
    S.candidates.forEach((c) => tbody.appendChild(candRow(c)));
  } else {
    tbody.appendChild(candRow());
    tbody.appendChild(candRow());
  }
  if (S.runs.length) { renderPrepareResults(); renderLaunchGuides(); }

  $("#add-cand").onclick = () => tbody.appendChild(candRow());
  $("#btn-prepare").onclick = doPrepare;
  $("#btn-collect").onclick = doCollect;
  $("#btn-report").onclick = doReport;
  $("#btn-prev").onclick = () => showStep(Math.max(1, S.step - 1));
  $("#btn-next").onclick = () => { if (canAdvance(S.step)) showStep(Math.min(5, S.step + 1)); };
  $("#btn-reset").onclick = () => {
    if (confirm("清空当前向导状态（不删除已生成的 run 目录与结果）？")) {
      localStorage.removeItem("ide-bench-wizard");
      location.reload();
    }
  };
  $$("#steps .step").forEach((el) => {
    el.style.cursor = "pointer";
    el.onclick = () => showStep(Number(el.dataset.step));
  });
  showStep(S.step || 1);
  if (S.jobId && S.step === 5) pollReport();
}

init();
