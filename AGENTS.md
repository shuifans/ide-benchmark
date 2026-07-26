# AGENTS.md — ide-benchmark 操作规程

> 本文件是**跨 agent 的唯一操作入口**。任何驱动本仓库跑测评的 agent 请严格按本文件操作。
> 人读版说明见 `README.md`。

一次测评（run）= 输入（任务提示词）+ agent 执行过程 + 输出产物；记录耗时/token/成本，
按「R 任务结果 45 + P 过程能力 30 + T token 成本 15 + D 时间成本 10」综合评分。
所有下游脚本只认归一化后的 `run.json` / `score.json`。

## 术语与目录

- `tasks/<task_id>/`：题库（入仓）。四件套：`task.yaml` / `prompt.md` / `workspace/` / `verifier/`。
- `runs/<run_id>/`：单次运行的工作区与原始日志（**已 .gitignore，仅本地**）。
- `results/runs/*.run.json`、`results/scores/*.score.json`、`results/analysis/*.json`：入仓的结构化结果。
- `reports/`：HTML 报告。
- 支持的 agent（harness）：`claude-code` / `codex` / `qoder-cli` / `opencode` / `pi` / `qwen` / `kimi`
  （见 `scripts/common.py` 的 `AGENTS`）。

## 环境

- Python 3，依赖 `pyyaml`、`jsonschema`。
- L2 judge 需 `config/judge.json`（从 `judge.example.json` 复制），OpenAI 兼容 API。
- Web 向导：`python web/server.py`（可代替下面第 1/3/4/5/6/7 步的手工命令）。

## 完整 run 生命周期

### 步骤 0：任务体检（建议级，不阻断）

```
python scripts/check_task.py <task_id>
```

### 步骤 1：初始化 run

```
python scripts/prepare.py --task <task_id> --agent <agent> --model <model> --context-window <cw> --thinking <t>
```

产出 `runs/<run_id>/`（`work/` 干净工作区、`PROMPT.md`、`manifest.json`），stdout 打印启动指引 JSON。

### 步骤 2：运行被测 agent

在 `work_dir` 内启动被测 agent，把 `PROMPT.md` 内容作为提示词交给它。各 harness 要点：

- **claude-code**：work 目录下启动 `claude`；确认 transcript 记录 token usage。
- **codex / pi / qwen / kimi**：**必须 cd 进 work/** 再启动（按启动目录归属 session）。
- **qoder-cli**：**必须**经 `claude-tap --tap-client qoder --tap-no-open` 包裹，否则采不到 token，该 run 作废。
- **opencode**：**必须 cd 进 work/** 再启动。

### 步骤 3：采集日志并归一化

```
python scripts/collect.py <run_id> [--transcript <path>] [--status completed|timeout|error]
```

- token 全 0 → 脚本报错，该 run 作废，回步骤 2 重跑。
- 模型不在价格表 → 先在 `scripts/pricing.py` 补价格/别名。

### 步骤 4：客观检查（L0）

```
python scripts/verify.py <run_id>
```

verifier 此步才复制进 `work/`，运行期对被测 agent 不可见（防作弊）。

### 步骤 5：过程信号（L1）

```
python scripts/process_metrics.py <run_id>
```

### 步骤 6：judge 盲评与对比分析（L2，自动）

```
python scripts/judge.py run <run_id>
python scripts/judge.py compare <run_id_a> <run_id_b> ...
```

- rubric：`config/rubric-v3.md`（四维 0–100 + flags 红线；融合 P1–P14 过程标准与任务档位敏感度）。
- 材料自动匿名化（agent/厂商标识 → `[agent]`）；compare 用 候选A/B/C 标签，结果缓存于 `results/analysis/`。
- 校验：`python scripts/validate.py --schema score results/scores/<run_id>.score.json`。

### 步骤 7：报告

```
python scripts/report.py single <run_id>
python scripts/report.py compare <run_id...> [--weights R45,P30,T15,D10]
```

## 硬规则（易错点，务必遵守）

- qoder-cli 未经 `claude-tap` 包裹 → 采不到 token → run 作废（本机需先安装 claude-tap）。
- codex / opencode / pi / qwen / kimi 未 cd 进 `work/` → session 归属错误 → 采不到日志。
- `collect.py` 报 token 全 0 → 该 run 作废，重跑。
- **kimi**：usage 已用真实 probe 会话校准（step.end 事件，2026-07-26）；probe 会话未含工具调用，
  首次真实任务 run 后如工具事件解析异常，对照 wire.jsonl 校准 `adapters/kimi.py`。
- **codex**：适配器按已知上游格式编写，本机未实测；首跑后如解析异常，用样例日志校准 `adapters/codex.py`。
- 修改 `tasks/<id>/prompt.md` 视为新任务：升 `task.yaml` 的 `prompt_version`，保证历史结果可比。
- 不要把 `runs/`（原始日志/产物）入仓；只提交 `results/` 与 `reports/`。
- `config/judge.json` 含 API key，已 gitignore，不得入仓。

## 新增任务

复制任意任务目录为模板（easy: `py-fix-off-by-one` / medium: `py-rate-limiter` / hard: `py-task-tracker`），
改四件套，跑 `check_task.py` 看建议。
