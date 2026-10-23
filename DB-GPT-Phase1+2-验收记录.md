# DB-GPT 上下文压缩 Phase 1+2 —— 交付与验收记录

> **用途**：给第三方验收人的自包含记录（对账用）。判据以仓库内文档为准：
> 1. 《[DB-GPT-实施交接Brief（Phase 1+2）](./DB-GPT-实施交接Brief（Phase 1+2）.md)》——任务包/红线/验收/报告格式；
> 2. 《[DB-GPT-上下文压缩改造设计（Phase 1+2）](./DB-GPT-上下文压缩改造设计（Phase 1+2）.md)》——**v2 权威设计**（与 Brief 冲突时以它为准）；
> 3. 《[DB-GPT-上下文压缩改造设计（Phase 0）](./DB-GPT-上下文压缩改造设计（Phase 0）.md)》——前置机制。
>
> **基线**：分支 `wip/rollout-ltm-pilot-20261013`，本批起点 `e608a6fd9`，交付终点 `a4d459496`（已推送 origin）。
> **行号锚点以 `a4d459496` 为准。** 标注约定：已核实 = 源码/命令一手核对；解释点 = 实现选择（见 §3）。

---

## 0. 交付摘要

| 项 | 值 |
|---|---|
| 提交组 | 6 组（组 4/5 同批合入，F5） |
| 提交 | `9f95fa951` → `327917668` → `362d89b7f` → `4e8f1f286` → `412ad0a16` → `a4d459496` |
| Flag | `context_compaction_enabled=false`、`context_compaction_dry_run=false`（默认关，行为与改动前一致） |
| 改动面 | 9 个生产文件 + 6 个测试文件（其中 3 个为新增测试；另新增 1 个生产模块 `transcript_summary.py`），`compact.py` 零改动 |
| 回归 | 三目录 + app 套件：`430 passed / 12 failed`（12 项与基线清单逐项一致）；`ruff` 全绿 |

---

## 1. 改动清单（文件 → 位置 → 设计章节 → 提交）

**组 1 · P1-① 去重 · `9f95fa951`**

| 文件 | 位置 | 章节 |
|---|---|---|
| `core/context/transcript_compaction.py` | `:44-46` 引用行常量；`:147-165` 调用映射/已处理标记；`:483-590` `dedupe_replacement`；`:862-` `decide_compaction`（去重先行 + 合并 accounting，新增 `dedupe_count`） | §2.1 / F1 / F8 |
| `budget.py` `:77`；`app/config.py`；`agentic_data_api.py` | `dedup_min_chars=200` 配置通道 | §4 |
| 测试 | `test_transcript_compaction.py` +199（8 例）；`test_rollout_compaction.py` +34（e2e） | §6.1 |

**组 2 · P1-② 泄压阀 · `327917668`**

| 文件 | 位置 | 章节 |
|---|---|---|
| `transcript_compaction.py` | `:74-88` `CompactionWindow.tail_tokens`（F3）；`:335-411` `select_window(pressure, pressure_stage)` 三阶段；`:862-` `decide_compaction` 正常→pressure→拒绝，`gate="pressure"` | §2.2 / F3 |
| 配置/日志 | `pressure_soft_factor=1.5`；日志 `pressure_stage` | §4 |
| 测试 | +193（tail_tokens 精度 / 触发 / A、B、C / 硬底线 / persisted / 回收门） | §6.2 |

**组 3 · P1-③ 一行剪枝 · `362d89b7f`**

| 文件 | 位置 | 章节 |
|---|---|---|
| `transcript_compaction.py` | `:200-222` `render_one_line`（`[{name}] args≈{args≤80} · success={t/f/?} · {N} chars`）；`:414-` `build_pruned_replacement`（净收益门 `min_saving_chars`；三标记幂等） | §2.3 |
| `rollout.py` | `:86` `tool_success` 投影映射；tool_result 记录 bool；replacement 不清理（F8） | §2.4 |
| 配置 | `min_saving_chars=500`、`prune_args_preview_chars=80` | §4 |
| 测试 | +151 / `test_rollout.py` +52（§6.3 六例 + 映射） | §6.3 |

**组 4 · P2-⑦ 摘要生产者 · `4e8f1f286`**

| 文件 | 位置 | 章节 |
|---|---|---|
| `transcript_compaction.py` | `:596-608` `SummaryRegion`；`:654-` `select_summary_region`（F2）；`:693-716` `build_summary_replacement`；`:718-` `assemble_summary_draft`；`:762-` `decide_summary` | §3.2/§3.3/§3.8 |
| `context/transcript_summary.py`（新） | `:130-` 序列化（全量）；`:155-` prompt；`:162-` 摘要消息（L3 前缀） | §3.3/§3.4 |
| `serve/.../rollout_compaction.py` | `:54-115` `SummaryFailureTracker`（§3.6）；`:187-` `_append_payload`；`:204-` 摘要 dry-run 日志；`:305-` `maybe_compact_async`（策略选择/失败回退/never-raise） | §3.1/§3.2/§3.6/§3.9 |
| `agentic_data_api.py` `:3286` | loader 包装改 `await maybe_compact_async(llm_client=agent.llm_client, llm_model=…)` | §3.1 |
| 测试 | `test_transcript_summary.py`（新）；`test_rollout_compaction.py` +336（策略/回退/冷却/dry-run/重启/请求视图） | §6.4 |

**组 5 · P2-⑧ 无帽 + trim-retry + 结束标记 · `412ad0a16`（与组 4 同批，F5）**

| 文件 | 位置 | 章节 |
|---|---|---|
| `agent/util/llm/llm_client.py` `:257-268` | 显式 `max_new_tokens=None` 透传（修复 `int(None)` 崩溃） | §3.5 / F7 |
| `transcript_summary.py` | `:102-107` 结束标记；`:122-` 超窗分类（8 模式）；`:176-` trim-retry（最老 turn 裁剪、1-turn 终止、F4） | §3.3/§3.4 |
| 新测试 | `agent/util/tests/test_llm_client_max_tokens.py`；`model/proxy/tests/test_openai_max_tokens.py`；trim-retry 3 例 | §6.4 / §0.3-7 |

**组 6 · P2-⑫ 模板三版本 · `a4d459496`**

| 文件 | 位置 | 章节 |
|---|---|---|
| `transcript_summary.py` | `:44-80` handoff/hybrid 模板；`:82-99` 名称/文本解析；`summarize_region` 按配置取模板 | §3.7 |
| 配置/日志 | `summary_template="hybrid"`；dry-run 日志 `would_template` | §4 / §3.9 |
| 测试 | 三值切换、默认/回退、实际 prompt 断言、日志字段 | §6.4 |

---

## 2. 测试（命令与原始输出摘要）

环境：仓库自带 `.venv`（Python 3.11.15）。

### 2.1 基线（改动前，`e608a6fd9`）与收工对比（`a4d459496`）
```
三目录（core/tests + context/tests + serve db tests）：130 passed → 179 passed
app 套件：                                              12 failed, 251 passed → 12 failed, 251 passed
```
（三目录 +49 全部为本批新增用例；app 套件数字不变、失败清单逐项一致。）
基线既有失败（12 项，本批未修，验收时按此清单比对）：

```
test_agentic_multi_file_flow.py::test_scheduled_task_freezes_then_replays_immutable_inputs
test_attachment_react_adapter.py::test_chat_react_agent_closes_attachments_when_stream_response_init_fails
test_attachment_react_adapter.py::test_chat_knowledge_agent_closes_attachments_exactly_once_when_stream_fails
test_skill_upload_api.py::test_skill_upload_rejects_traversal_filename
test_skill_upload_api.py::test_skill_upload_rejects_absolute_filename
test_skill_upload_api.py::test_skill_upload_rejects_windows_path_separator
test_workspace_provisioning.py::test_ensure_pilot_workspace_copies_alembic_ini
test_workspace_provisioning.py::test_ensure_pilot_workspace_copies_alembic_env_py
test_workspace_provisioning.py::test_ensure_pilot_workspace_copies_alembic_script_mako
test_workspace_provisioning.py::test_ensure_pilot_workspace_copies_benchmark_xlsx
test_workspace_provisioning.py::test_ensure_pilot_workspace_idempotent_no_overwrite
test_workspace_provisioning.py::test_ensure_pilot_workspace_creates_missing_directories
```

### 2.2 目标命令（§10，收工状态 `a4d459496`）
```
.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests/test_transcript_compaction.py \
  packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py \
  packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_rollout_compaction.py -q
  → 86 passed

.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests/test_transcript_summary.py -q
  → 18 passed
```

### 2.3 推荐回归范围（§10）
```
.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/tests \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests \
  packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests \
  packages/dbgpt-app/src/dbgpt_app/tests -q
  → 12 failed, 430 passed     （失败 = 上述基线 12 项，逐项一致）
```

### 2.4 追加
```
.venv/bin/python -m pytest packages/dbgpt-core/src/dbgpt/agent/util/tests \
  packages/dbgpt-core/src/dbgpt/model/proxy/tests -q
  → 22 failed, 67 passed
```
22 项全部为 `ModuleNotFoundError: No module named 'litellm'`（环境缺可选依赖，与本批无关；本批新增的 openai 出站断言在通过之列）。

### 2.5 Lint
```
.venv/bin/python -m ruff check <本批全部改动文件>   → All checks passed!
```

### 2.6 验收标准（设计 §0.3 八条）对照
1. §6 单测全绿、无新增失败 → 2.2–2.4 如上；
2. `flag=off` 与改动前一致 → 既有回归全过；禁用时协调器不被调用（`test_disabled_feature_is_a_strict_no_op`）；
3. dry-run：P1 照常试算；P2 不调 LLM、不写事件（`test_dry_run_summary_records_the_plan_without_calling_the_llm` 断言 mock 0 调用、0 事件）；
4. `flag=on`：一行格式含 success、摘要事件写入、投影中段摘要、重启重建一致（`test_summary_survives_a_process_restart_rebuild`）；
5. trim-retry：成功 / 有界放弃（1-turn 终止、总调用上限、墙钟预算、0 值关闭时钟）各一例（`test_trim_retry_*`/`test_time_budget_*`）；
6. 模板三值切换（`test_summary_template_switch_selects_all_three_variants`）；
7. 无帽按部署形态（远程 proxy）：AIWrapper 保留 None → `to_dict()` 丢弃 → 出站 payload 无 `max_tokens`（两层断言）；
8. 兼容幂等：旧截断痕迹/引用行/一行格式均不被二次处理（`test_already_processed_contents_are_never_rewritten`）。

---

## 3. 与设计文档的偏差

无结构性偏差；实现级解释点（供评审拍板）：

1. **截断门槛**取 `min_prune_chars`（默认 8000，clamp≥200）作为“够格被剪”的门槛；§3.6“压缩内 200”按 Hermes 档位遗产/Phase 2 用途理解，未消费。

1b. **F4 已修订（评审后）**：`summary_trim_max_attempts` 原语义（“只约束无进展重试”）在“每轮必裁一个 turn”的实现下数学上不可触发（复现：12 turn → 12 次调用，cap=8）。现重定义为 **trim 循环的 LLM 调用总上限（含首次，默认 8）**，并新增 **`summary_time_budget_seconds`（默认 60，0=关闭）** 墙钟预算（每次调用前检查；不中断进行中的调用）。三个终止条件并存：只剩 1 turn / 总次数 / 墙钟。**设计文档的 F4 修订建议见 §7 附录，待并入。**
2. **摘要区域与“无主段”**：普通无主段阻断区域；**带摘要标记的无主段可吸收**（F2“旧摘要段不阻断”与“无主段即停止”仅在此时兼容）。
3. **泄压阀“够用即停”按阶段粒度**（A/B/C 逐级、达标即停；阶段内不做逐条增量停止）。
4. **dry-run 日志分流**：摘要门控通过 → §3.9 摘要日志；未通过 → Phase 0 prune dry-run 日志格式。
5. **`summary_chars`** = 摘要消息完整内容长度（含前缀/结束标记）；摘要 accounting 按 §3.8 四键；P1 accounting 增加 `dedupe_count`。
6. **无帽仅覆盖远程 proxy 部署形态**（openai/tongyi）；本地后端（mlx/vllm/hf）保持既有默认帽，未支持“显式 None”（非本部署形态）。
7. `model/proxy/tests/` 仅新增测试文件（出站断言，§0.3-7 要求）；模型层生产代码未动。
8. `maybe_compact`（同步）按 §3.1 保留为“仅剪枝内核”+ 兼容测试；app 挂载已切换为 `maybe_compact_async`。

---

## 4. 红线核验（复核命令）

```bash
# compact.py 零改动（应为空）
git diff e608a6fd9..a4d459496 --stat -- '*compact.py'

# 前端/静态资源零改动（应为空）
git diff e608a6fd9..a4d459496 --stat -- '*/web/*' '*/static/*'

# flag 默认关（应输出 False/False）
grep -n "context_compaction_enabled: bool = False\|context_compaction_dry_run: bool = False" \
  packages/dbgpt-core/src/dbgpt/agent/core/context/budget.py
```
已核实结果：两条 diff 均为空；flag 默认 `False`。未做剪枝落盘、未建 per-tool 模板、未动其他工作流（pilot/长期记忆/模型层生产代码）。

---

## 5. 未决 / 风险

- **待拍板**：§3 的解释点 1、2（阈值口径、无主段摘要吸收）。
- **灰度数值未定**：`proactive_prune_tokens` / `compact_threshold_ratio` 需 dry-run 观测后确定；当前默认全关。
- **摘要调用在请求关键路径**：达阈值时每步最多一次 LLM 尝试（内部受总调用上限 8 与墙钟 60s 约束，见 §7）；失败/超时回退剪枝；cooldown（3 次/60s）防风暴。摘要耗时已进日志（`summary_seconds` / `summary_llm_calls`，见 §7）。
- **F4 设计文档尚未并入修订**（当前设计文档仍为旧语义）；已按 §7 附录实施代码，待设计文档同步。
- **超窗分类为文本匹配 best-effort**（8 模式），失败时日志记错误前 200 字符。
- **已知局限**：pressure 不覆盖“未闭合调用 turn 内”的死锁；去重引用行有残留场景（均经评审接受、记录不修）。
- **验证边界**：未起真实服务/真实 provider 做端到端；结论止于单测/集成测试层。灰度（dry-run → 小流量 → 默认）由部署方执行。

---

## 6. 复跑指引（验收人）

```bash
cd /Users/zsm/Documents/DB-GPT
git log --oneline e608a6fd9..a4d459496          # 应见 6 个提交
git status --short                              # 仅 pilot/ 的两处并发改动（不属于本批）

.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/tests \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests \
  packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests \
  packages/dbgpt-app/src/dbgpt_app/tests -q     # 12 failed / 430 passed（失败=基线清单）
```

---

## 7. 评审修复（后续提交 `fix(agent): bound summary trim retries...`）

针对外部评审的 5 条（第 2/3/4/6/7 条）逐条处理：

| 评审项 | 结论与处置 | 落点 |
|---|---|---|
| ② `summary_trim_max_attempts` 失效 | 属实（已复现 12 turn → 12 调用）。根因：F4 原语义在逐 turn 裁剪下不可触发，且 Codex 原文的 `max_retries` 约束瞬时错误而非裁剪。**重定义为总调用上限（含首次，默认 8）**，新增**墙钟预算 `summary_time_budget_seconds`（默认 60，0=关）**；三个终止条件并存 | `transcript_summary.py::summarize_region`；`budget.py`/`app/config.py`/loader |
| ③ 决策/组装在事件循环上 | 属实（偏离 §3.1）。`decide_compaction`/`decide_summary`/`assemble_summary_draft` 已移入 `run_in_threadpool` | `serve/.../rollout_compaction.py::maybe_compact_async` |
| ④ 摘要时长无仪表 | 属实（已披露项）。现在提交/失败日志携带 `summary_seconds` 与 `summary_llm_calls` | 同上 |
| ⑥ 本地后端 2048 默认帽 | 属实且为已披露项；当前部署形态（远程 proxy）不受影响。**不修**，仅保留文档说明（修它需动模型层，超出本批红线） | 部署说明（本条） |
| ⑦a 泄压阀失败缺 best 阶段 | 属实。失败分支现在记录产生 best 试算的 `pressure_stage` | `transcript_compaction.py::decide_compaction` |
| ⑦b 文件计数错误 | 属实。已更正为 9 个生产文件 / 6 个测试文件（3 新增） | 本记录 §0 |
| ⑦c 全局 vs 同 turn 工具映射 | 无影响（provider `tool_call_id` 全会话唯一，两种映射等价），不改 | — |

新增/更新测试：总调用上限（12 turn → 8 次、异常上抛）、墙钟预算（伪时钟：首次调用后不再发起）、`0` 值关闭时钟、短区域仍按 1-turn 终止（3 turn → 3 次）、提交日志含耗时/调用数；pressure 失败分支的 stage 断言更新为 best 阶段。

### 附录：F4 修订建议（供并入《DB-GPT-上下文压缩改造设计（Phase 1+2）》）

> **原文**：终止条件 = 只剩 1 个 turn；`summary_trim_max_attempts`（8）只约束“无进展的重试”次数（有进展则该计数清零——Codex 语义）。
>
> **修订为**：trim 循环由三个终止条件共同约束（先到者生效）：
> 1. 只剩 1 个 turn（无物可裁）；
> 2. `summary_trim_max_attempts` = 本轮 trim 循环的 **LLM 调用总上限（含首次调用，默认 8）**——达到即放弃、回退剪枝；
> 3. `summary_time_budget_seconds`（默认 60，`0`=关闭）= 墙钟预算，**每次发起调用前**检查；客户端无单次调用超时参数，预算只阻止“下一次”调用，不中断进行中的调用。
>
> **修订理由**：原“无进展重试”语义在“每轮必裁一个 turn”的实现下 `no_progress` 恒为 0、参数形同虚设（已复现）；且 Codex 原文的 `max_retries` 约束的是非超窗瞬时错误（带退避），与裁剪循环无关——本实现非超窗失败直接走失败路径，故瞬时重试预算在本实现中没有消费者。
>
> **代价与残留**：`turns` 大于上限的区域会在第 N 次调用后放弃（可能少裁若干 turn，仅影响“尝试成功的机会”）；失败后回退的 prune 在完整投影上进行，无半成品残留。
