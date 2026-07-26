# ide-benchmark — AI 编码工具综合测评平台

对比测评 AI 编码 CLI/IDE 工具（claude-code / codex / qoder-cli / opencode / pi / qwen / kimi）
在同一任务上的表现，同时考察四个维度：

| 维度 | 权重（默认） | 说明 |
|---|---|---|
| R 任务结果 | 45 | verifier 客观通过率 35 + judge 代码质量 10 |
| P 过程能力 | 30 | 规划 / 执行纪律 / 自测验证（harness 能力标准，各 10） |
| T token 成本 | 15 | 总 token 组内 min-max 归一（越省越高） |
| D 时间成本 | 10 | 耗时组内归一（越快越高） |

> 质量门槛：verifier 全挂或运行异常的 run，T/D 记 0，防止"快速失败"骗效率分。
> 权重可用 `--weights R45,P30,T15,D10` 调整。

由 [agent-coding-bench](https://github.com/shuifans/agent-coding-bench)（三层取证管线）与
[ai-ide-benchmark](https://github.com/shuifans/ai-ide-benchmark)（过程质量方法论）整合而来。

## 三层取证

1. **L0 客观检查**：任务自带 pytest verifier，运行期对被测 agent 不可见（防作弊），确定性打分；
2. **L1 过程探测**：脚本从归一化日志提取客观行为信号（是否规划、范围符合率、末编辑后自测、无效重试等）；
3. **L2 judge 盲评**：LLM 按固定 rubric（`config/rubric-v3.md`，融合 P1–P14 过程标准与红线规则）盲评四维，
   并撰写对比分析正文。

## 快速开始（Web 向导）

```bash
pip install pyyaml jsonschema
cp config/judge.example.json config/judge.json   # 填写 judge 模型 API（OpenAI 兼容）
python web/server.py                              # 打开 http://127.0.0.1:8321/
```

五步向导：
1. **选择任务**：按复杂度 easy（bug 修复）/ medium（功能实现）/ hard（SPEC 驱动应用）分档；
2. **生成测试环境**：为每个「agent+模型+参数」候选生成独立 run 目录与提示词（一键复制）；
3. **运行任务**：按页面指引在各编码工具中跑任务（含每个工具的启动命令与硬规则提示）；
4. **确认采集**：全部跑完后一键采集 → 归一化 token/耗时 → verifier 客观检查 → 过程信号提取；
5. **生成报告**：自动 judge 盲评 + LLM 对比分析 → HTML 对比报告。

报告包含：任务说明、每个候选的测试环境（agent+model+思考强度）、测评指标词汇表、
LLM 撰写的对比分析正文、可展开的原始测试数据（默认收起）。

## 命令行用法

```bash
python scripts/check_task.py <task_id>                         # 任务体检（建议级）
python scripts/prepare.py --task <task> --agent <agent> \
    --model <model> --context-window 200k --thinking high      # 初始化 run
# ……在 work 目录用被测工具跑任务……
python scripts/collect.py <run_id>                             # 采集日志归一化
python scripts/verify.py <run_id>                              # L0 客观检查
python scripts/process_metrics.py <run_id>                     # L1 过程信号
python scripts/judge.py run <run_id>                           # L2 盲评
python scripts/judge.py compare <run_id...>                    # 对比分析正文
python scripts/report.py compare <run_id...>                   # HTML 对比报告
```

## 支持的被测工具与日志采集

| Agent | 启动方式 | 日志来源 | token |
|---|---|---|---|
| claude-code | work 目录下 `claude` | `~/.claude/projects/<slug>/*.jsonl` | ✓ |
| codex | cd work 后 `codex` | `~/.codex/sessions/Y/M/D/rollout-*.jsonl` | ✓（格式待实测校准） |
| qoder-cli | **必须** `claude-tap --tap-client qoder --tap-no-open` 包裹 | claude-tap traces.sqlite3 | 仅经 claude-tap |
| opencode | **必须 cd 进 work** 后 `opencode` | `~/.local/share/opencode/opencode.db` | ✓ |
| pi | cd work 后 `pi` | `~/.pi/agent/sessions/<esc-cwd>/*.jsonl` | ✓ |
| qwen | cd work 后 `qwen` | `~/.qwen/projects/<esc-cwd>/chats/*.jsonl` | ✓ |
| kimi | cd work 后 `kimi --yolo` | `~/.kimi-code/sessions/wd_*/session_*/agents/main/wire.jsonl` | ✓（已 probe 校准） |

硬规则（违反则 run 作废）：
- qoder-cli 不经 claude-tap → 采不到 token；
- opencode / pi / qwen / kimi 必须在 work 目录内启动（按启动目录归属 session）；
- `collect.py` 报「token 全 0」→ 该 run 作废，重跑。

## 任务库

| 任务 | 难度 | 考察点 |
|---|---|---|
| py-fix-off-by-one | easy | 精准定位与克制修复 |
| py-rate-limiter | medium | 计划先行 + TDD + 边界语义（窗口边界/配额/隔离） |
| py-task-tracker | hard | SPEC 追溯 + 任务清单维护 + 分层测试（持久化/损坏恢复/导入幂等） |

新增任务：复制任意任务目录为模板，改四件套（`task.yaml` / `prompt.md` / `workspace/` / `verifier/`），
跑 `check_task.py` 看建议。修改 prompt.md 视为新任务（升 `prompt_version`）。

## 目录结构

```
adapters/     各 harness 日志适配器（归一化为统一 events.jsonl）
scripts/      管线：prepare → collect → verify → process_metrics → judge → report
schemas/      manifest / run / score 的 JSON Schema
config/       rubric-v3.md、judge.example.json（judge.json 不入仓）
tasks/        任务库（四件套）
web/          五步向导 Web 服务（stdlib，零额外依赖）
runs/         单次运行工作区与原始日志（gitignore，仅本地）
results/      入仓的结构化结果（run.json / score.json / analysis）
reports/      HTML 报告
```

## 开发

```bash
python -m pytest tests/   # 管线与适配器测试
```
