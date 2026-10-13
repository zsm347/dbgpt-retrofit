# DB-GPT Phase 0 实施交接 Brief —— 全量版（步骤 1+2+3 一次交付）

> **用途**：本文件是**自包含**的施工任务书（**取代**《…（Phase 0-步骤1）》单步版）——把它 + 下面两份配套材料交给执行 agent，可**一次完成 Phase 0 的全部代码**，不需要任何额外对话上下文。
>
> **配套材料（都在仓库里，阅读顺序）**：
> 1. 《[DB-GPT-上下文压缩改造设计（Phase 0）](./DB-GPT-上下文压缩改造设计（Phase 0）.md)》——**完整设计（v2.1，权威）**，凡本 Brief 与它冲突，以它为准；
> 2. 《[DB-GPT-主链路评估与改进清单](./DB-GPT-主链路评估与改进清单.md)》——D.15（现状复核）/ D.16（路线图）/ P1-25。
>
> **本任务不含步骤 4（dry-run 灰度）**——那是代码合入后的**上线程序**，由部署方执行（流程见 §9.3）。
>
> 标注约定（沿用上述文档）：**已核实** = 源码一手核对（带 `file:line`）；**推断** = 合理推理未验证；**建议** = 设计选择。执行方报告时同样遵守。

---

## 0. 背景与任务一句话

**背景（已核实，详见 D.15）**：DB-GPT 主链路已切到 rollout 事件投影（原生 `role=tool` 消息、每步 `load_projection` 重建请求），但压缩体系仍按旧文本 ReAct 设计——L1/L2/L3 全部不触发、压缩结果未接入事件流。Phase 0 = **修地基**。

**任务一句话**：一次性完成 **Phase 0 全部代码 = 步骤 1（投影/事件层）+ 步骤 2（P1-19 ① / P1-23 接线修复）+ 步骤 3（协调器 + 最小生产者 + 触发）**，含全部测试与三组提交。**flag 保持默认关、dry-run 能力就绪；不改 legacy 路径；不做 Phase 1/2 的任何内容。**

**依赖关系（决定实现顺序）**：

```
任务包 A（投影/事件层）──→ 任务包 C（协调器依赖 A 的 schema）
任务包 B（接线修复）    ──→ 独立，可先行（建议排第二组提交）
```

---

## 1. 仓库与基线（⚠️ 开工前必做）

| 项 | 值 |
|---|---|
| 仓库 | `/Users/zsm/Documents/DB-GPT` |
| **基线分支** | `wip/rollout-ltm-pilot-20261013`（**工作区应为 clean——`git status --short` 应为 0 项**） |
| 基线提交 | 以开工时 `git log --oneline -3` 为准（最新应含 `docs: remove L3 content[:3000] truncation ...`） |
| 远程 | `origin` = `https://github.com/zsm347/dbgpt-retrofit`（**私有，可推送**）；`upstream` = eosphoros-ai/DB-GPT（**只读参考——禁止 push**） |
| 网络提示 | 本机 git 访问 GitHub 需走代理（gh 命令不受影响）：`git -c http.proxy=http://127.0.0.1:7897 <命令>` |
| 身份提示 | 仓库未配置 git 身份 → 提交请用一次性参数：`git -c user.name=zsm347 -c user.email=zsm347@users.noreply.github.com commit -m "..."` |

**纪律**：
- ✅ 开工/收工各做一次 `git status --short`（收工时只应多出你的改动）
- ✅ 你的改动提交到 `wip/rollout-ltm-pilot-20261013`；可推送（代理参数）；提交信息用英文（仓库风格 `feat(agent): ...`）
- ❌ 禁止 push 到 `upstream`；禁止 `reset --hard` / `rebase` 既有提交；禁止删除 `.gitignore` 中新增的排除规则
- ❌ 不修改**其他工作流**已提交的文件（长期记忆、pilot、模型层）——除非任务明确要求

---

## 2. 事实来源（只信设计文档，不要重新发明）

| 需要什么 | 看哪里 |
|---|---|
| 事件 schema 与校验规则 | 设计文档 **§4.1 / §4.2** |
| 投影应用伪代码 | §4.3 |
| 投影类扩展（`latest_event_seq` / `last_compaction`） | §6.2（F1） |
| turn 判定 / 保护区 / 生产者输出格式 | §3.1–§3.6 |
| 协调器时序 / 状态映射 | §2.3（v2）/ §2.4 / §2.5（F8） |
| 触发与配置（含窗口夹紧 F5） | §5.1 / §5.2 |
| rearm 决策 | §6.2 / §6.3 |
| P1-19 / P1-23 | §7.1 / §7.2 |
| 测试与验收 | §9 |
| **已拍板决策（不得改动）** | **§11** |

⚠️ 遇到"设计文档与代码不符" → **停下来报告**，不要自行改设计、不要"顺手修"。

---

## 3. 任务包 A：投影 / 事件层（提交组 1；零行为变化）

### A1. `packages/dbgpt-core/src/dbgpt/agent/core/rollout.py`

1. `_SUPPORTED_EVENT_TYPES`（`:17`）加入 `"context_compacted"`
2. 新增投影分支（伪代码见设计文档 §4.3）：
   - 遇 `context_compacted` → 丢弃已累积的 `projected`，**整体替换**为 `replacement_history` 转换出的 `ModelMessage` 列表
   - 防御性校验（转换前逐条检查）：① role ∈ {human, ai, tool}（**无 system**）② content 为 str ③ 载荷内部配对闭合——任一不满足 → `ValueError`（与既有"畸形历史抛错"契约一致，`rollout.py:85-86` 风格）
   - ⚠️ 新类型**必须同时新增分支**，否则会落入 `tool_result` 分支报错（`rollout.py:151` 注释："The only remaining supported non-lifecycle event is tool_result."）
3. 扩展 `ChatCompletionHistoryProjection`（`:42-46`）：
   - 新增 `latest_event_seq: int | None`（最后一个事件的 seq）
   - 新增 `last_compaction: CompactionState | None`（最近一次压缩的 `{rearm_tokens, through_event_seq, accounting}`；从未压缩过 = `None`）
   - 新增 `CompactionState` dataclass（定义在 `rollout.py` 内；该投影类全仓仅一个构造点 `rollout.py:174`，改动风险低）
4. 投影过程中顺带计算上述两个字段（投影器本就遍历全部事件，零额外成本）
5. replacement 允许角色：**human / ai / tool——禁止 system**（system 由请求组装层单独注入，`tool_calling_agent.py:512-545`）

### A2. `packages/dbgpt-serve/src/dbgpt_serve/agent/db/agent_event_db.py`

1. `_EVENT_TYPES`（`:35`）加入 `"context_compacted"`
2. `_validate_payload`（`:433` 起）新增分支，规则（设计文档 §4.2）——注：类型白名单（`_EVENT_TYPES`）与 `schema_version ≥ 1` 的通用检查已在入口 `_prepare_draft`（`:406`，`:409-410` 拒绝未知名）完成，这里只需加本类型的 payload 规则：
   - `schema_version`：int（当前只接受 `1`；版本演进策略见设计文档 §4.6）
   - `reason ∈ {proactive, threshold, manual}`；`strategy ∈ {prune, summary, hybrid}`
   - `replacement_history`：非空 list；每元素 `role ∈ {human, ai, tool}`；`content` 为 str（有 `tool_calls` 时允许 `""`）
   - `tool_calls`：复用 `assistant_message` 的形状校验（id 非空且唯一、`function.name` 为 str、`arguments` 为 str）
   - `tool_call_id`：非空
   - **内部配对闭合**：每个 `tool_call_id` 都有对应 ai 调用 id；未闭合 → 拒绝
   - `covers.through_event_seq`：int；`accounting`：dict；`rearm_tokens ≥ 0`；`created_by`：dict
3. 不改 append 的其他路径（未知名仍按现状拒绝）

### A3. 测试

`packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py` 新增：

1. **无压缩事件** → 投影与现状一致（回归基线）
2. **一次压缩** → 替换生效、消息序列正确、配对闭合
3. **多次压缩** → 后者整体覆盖前者（含审计字段顺序）
4. **畸形载荷**（未闭合配对 / system 角色 / 非法 role / 非 str content）→ `ValueError`
5. `latest_event_seq` / `last_compaction` 计算正确（含"从未压缩过" = `None`）

`packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_agent_event_db.py` 新增：

6. `context_compacted` payload 校验的**接受 / 拒绝矩阵**
7. 未知名事件仍被拒绝（回归）

---

## 4. 任务包 B：接线修复（提交组 2；独立可回滚）

### B1. P1-19 ①（窗口自适应派生）

在两处 `ContextBudgetConfig` 构造点：
- `openapi/api_v1/agentic_data_api.py` 的 `_load_context_budget_config`（`:170-195` 区域）
- `agent/core/base_agent.py` 的 `init_context_management` fallback 分支（`:87-96`）

用 `tool_result_budget_for_context_window(max_context_tokens)`（`agent/core/context/storage.py`，Hermes 移植的现成函数）**派生** `tool_result_threshold` 与 `turn_result_budget`——修复小窗口（32k/65k）上固定 100k 阈值吃爆窗口的问题（#23767 场景）。

⚠️ **P1-19 ② 已定不接线**：**不要**调用 `enforce_turn_budget`（`storage.py:382`）——决策 b3，回访条件见设计文档 §7.1 ②。

### B2. P1-23（read_file 帽）

`openapi/api_v1/tools/read_file.py`：
- schema：`limit` 增加上界（Hermes 对齐：**maximum = 2000**）
- 内部：单次输出的**字符帽（~100k）**；超帽截断并提示 `offset=...` 续读（现有续读提示见 `read_file.py:141-145`）

### B3. 测试

- P1-19 ①：构造点断言派生值（给定窗口 → 期望阈值）
- P1-23：大文件 + 大 limit → 输出有界、续读可用

---

## 5. 任务包 C：协调器 + 最小生产者 + 触发（提交组 3；依赖 A）

### C1. 新文件 `packages/dbgpt-core/src/dbgpt/agent/core/context/transcript_compaction.py`（纯函数）

- `detect_turns`：按设计文档 §3.2 判定表（turn 起点=human；内容=其后 ai/tool；system/view 不参与）——覆盖 §3.3 的 8 个边界用例
- `select_window`：§3.4——保护 token 预算 + 轮数下限 + 封顶 8；**受保护 turn** = ① 当前 turn（不变量 I1）② 含未闭合调用（synthetic id，F2）③ 无主段 ④ 摘要标记所在段
- `build_pruned_replacement`：§3.6 输出格式——`<persisted-output>` **原样跳过**；其他超门槛结果截断为 `前 {N} 字符 … [truncated, total {total} chars]`（**不带路径**）；幂等守卫（已处理过的痕迹不被二次处理）
- `estimate_tokens`：§9.5 口径（`ceil(Σ len(content)/4) + Σ ceil(len(json.dumps(tool_calls))/4)`，投影消息本体）

### C2. 协调器 `maybe_compact`（调用点固定在 loader 包装内）

- **挂载点（F4）**：`agentic_data_api.py:3156-3158` 的 `_load_rollout_projection` 包装内——顺序：`assert_turn_lease()` → `maybe_compact()` → `load_projection()`（返回的即是压缩后视图，当前请求直接受益）；`on_model_call`（`:3176-3177`）保持原样
- **门控**（伪代码见设计文档 §5.2）：
  - `effective_watermark = min(proactive_prune_tokens, window × 0.4)`（F5）
  - rearm 门读 `projection.last_compaction.rearm_tokens`
  - 未闭合调用 → **保护对应 turn**（F2），不再整体跳过
  - 回收量门：`reclaimed ≥ min_reclaim_tokens`
- **提交**：append `context_compacted`（payload 按 §4.1；`covers.through_event_seq = projection.latest_event_seq`）；**append 失败 = 无变化**（租约内、序号分配、原子）
- **dry-run（灰度第一档，配置驱动）**：`context_compaction_dry_run=true` 时——全流程运行（门控 + 试剪 + 回收量测量）但**不 append、投影不变**；输出一条**结构化日志**（建议前缀 `context_compaction` + JSON 字段：`est_tokens / watermark / gate / would_reclaim / would_prune_count`），供 §9.4 运行时指标统计
- **状态发射**：§2.5 的 state 新映射（`normal / low_watermark_active / threshold_reached / overflow`）

### C3. 配置（`agent/core/context/budget.py` + TOML 通道）

- §5.1 全部字段进 `ContextBudgetConfig`（默认全关/兼容）+ 加载通道（`_load_context_budget_config` + app TOML `agent_context` 段）
- `context_compaction_dry_run`（bool，默认 false）一并实现
- `compact_threshold_ratio` 是 **Phase 0 死配置**（F9）——标注"预留（Phase 2 摘要生产者接入后消费）"，不要为它写逻辑

### C4. `base_agent.py`：rollout 模式跳过 legacy `manage_context`（§2.4）

`_load_thinking_messages` 在 rollout 模式下不再调用 `manage_context`（避免无效计算与误导性状态）；**legacy 路径（非 rollout）行为保持不变**。

### C5. 测试

- §9.1：触发链四条门（水位 / rearm / 未闭合保护 / 回收量）各自的**通过与拒绝**路径
- producer：截断正确、保护区不动、persisted-preview 跳过、幂等
- dry-run：**不产生任何事件**、日志字段完整
- §9.2 集成：事件流 → 协调器 → append → 重投影 → `_thinking_native` 请求包含压缩视图（"第一步验证"断言的反转版）；**进程重启后仅凭事件流重建 → 压缩视图一致**；lease 丢失（`TurnLeaseLost`）→ 无变化；mid-turn 连续两次压缩 → 后者覆盖前者；含 synthetic 的会话（F2）→ 保护该 turn、其余可压
- §9.3 回归：flag=off 与现状一致；legacy 链路不受影响

---

## 6. 提交分组（保持可审性）

```
提交组 1：任务包 A（投影/事件层）        ——零行为变化（尚无生产者）
提交组 2：任务包 B（P1-19 ① / P1-23）   ——独立，可单独回滚
提交组 3：任务包 C（协调器+生产者+触发） ——flag 默认关 = 零行为变化
```

每组一个提交或若干小提交；提交信息英文（`feat(agent): ...` / `fix(agent): ...`）。

---

## 7. 明确不做（本期红线）

- ❌ **P1-19 ②**（`enforce_turn_budget` 不接线）
- ❌ **Phase 1/2 的一切**：去重、Pass 2/4、消息数封顶、trim-retry、模板 A/B、去 wire cap、输入有界化、移除 `content[:3000]`——**`compact.py` 本期不改**（legacy 路径与函数原样保留；新模块参考其 L1 既有行为：跳过 `<persisted-output>`、截断文案）
- ❌ 不开 flag（保持默认关）；不改 legacy 路径行为；不动 UI / 前端 / SSE 既有字段
- ❌ 不做无关重构（格式化、改名、依赖升级一律不做）

---

## 8. 环境与命令

```bash
cd /Users/zsm/Documents/DB-GPT
uv sync                                        # 环境未就绪时

# 目标测试
uv run pytest packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py -v
uv run pytest packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_agent_event_db.py -v
uv run pytest packages/dbgpt-core/src/dbgpt/agent/core/context/tests -v

# 收工前的相关回归范围（推荐）
uv run pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/tests \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests \
  packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests -v

# 仓库标准命令（Makefile:88 = `pytest --pyargs dbgpt`）——主要覆盖 `dbgpt` 包；
# serve / app 的测试请用上面的显式路径（CONTRIBUTING.md:119）
make test
```

---

## 9. 验收标准

### 9.1 代码验收

1. 新增单测（A3 / B3 / C5）全部通过
2. 既有测试**无新增失败**（基线若有既有失败，先记录并报告，不要顺手修无关问题）
3. 三组提交完成、工作区 clean、提交信息与分组对应

### 9.2 行为验收（三态）

| 配置 | 期望行为 |
|---|---|
| `enabled=false`（默认） | 与改动前**逐字节一致**（回归测试证明） |
| `enabled=true, dry_run=true` | 协调器全流程运行；**零事件写入**；结构化日志出现 |
| `enabled=true, dry_run=false` | 压缩事件正常写入；投影三态（无/一次/多次）正确；请求包含压缩视图 |

### 9.3 交付后的上线程序（部署方执行，不属本任务）

```
① dry-run 观察：开启 enabled+dry_run，收集日志（压缩频率/预计回收量/门控拒绝原因分布）
② 小流量提交：dry_run=false 开小比例（如按会话白名单）→ 观察压缩效果与 SSE 状态
③ 默认开启
```

---

## 10. 交付物格式（报告模板）

```
① 改动清单：文件 → 行区间 → 对应设计文档章节
② 测试：命令 + 结果（原始输出摘要，红/绿明确）
③ 与设计文档的偏差：有 / 无（有则逐条说明）
④ 未决 / 风险
⑤ git status --short 前后对比 + git log 新增提交列表
```

---

## 11. 纪律

- 结论分"**已核实 / 推断 / 建议**"三类；禁止无证据的"完成"汇报，每条验收必须贴命令与输出
- 改动与设计文档章节一一对应；不夹带无关改动
- 设计与代码不符、或触发任何红线 → **停止并报告**，等待指示
