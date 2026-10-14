# DB-GPT 上下文压缩改造设计（Phase 0）

> **范围与版本**：设计文档（非实现报告），基线 DB-GPT `ca9f014` 工作区（2026-10-13）；与《[DB-GPT-主链路评估与改进清单](./DB-GPT-主链路评估与改进清单.md)》的 **D.15 / P1-25 / D.16** 配套。本文只覆盖 D.16 的 **Phase 0**：结构检测重写、触发模型重划、压缩事件化机制、P1-19/P1-23 接线。**未改代码**。
>
> 标注约定：**已核实** = 源码一手核对（带 `file:line`）；**建议** = 设计提案；**待评审** = 需要拍板的决策点（汇总见 §11）。
>
> **v2 修订（2026-10-13）**：按自评审 F1–F12 修订——① 投影对象扩展 `latest_event_seq` / `last_compaction`（F1）；② 合成门改为"保护含未闭合调用的 turn"（F2）；③ `replacement_history` 禁止 system 角色（F3）；④ 协调器挂载点改为 loader 包装（F4）；⑤ 低水位按窗口夹紧（F5）；⑥ 截断文案与路径来源明确（F6）；⑦ 滚动发布纪律（F7）；⑧ 状态语义映射（F8）、死配置标注（F9）、不变量断言（F10）、测试补充（F11）、记账口径（F12）。
>
> **2026-10-13 决策落定（v2.1）**：估算器 = **独立 chars/4**（§9.5）；P1-19 ② = **b3 先不接线**（§7.1 ②）；P1-19 ① / P1-23 **保留**；UI read model **不动**；void **暂不做**（schema 预留）；向前兼容 = **部署纪律**（§4.6 方案 a）。原 §11"未决项"已全部转为决策记录。
>
> **实施交接 Brief（步骤 1）**：《[DB-GPT-实施交接Brief（Phase 0-步骤1）](./DB-GPT-实施交接Brief（Phase 0-步骤1）.md)》——自包含施工任务书，可直接交给执行 agent（含仓库基线红线 / 任务包 / 环境命令 / 验收 / 报告模板）。
>
> **Phase 1+2 实现级设计（2026-10-14）**：《[DB-GPT-上下文压缩改造设计（Phase 1+2）](./DB-GPT-上下文压缩改造设计（Phase 1+2）.md)》——去重 / 泄压阀 / 剪枝形态（激进一行）+ 摘要生产者 / 无硬帽 / trim-retry / 模板三版本。

---

## 0. 目标、非目标与验收标准

### 0.1 目标（Phase 0 完成后可达到）

1. **压缩层在 rollout 结构上复活**：结构检测基于 `ModelMessage`（`role=ai/tool/human`），不再依赖 `Observation:` 文本
2. **压缩结果可落地**：`context_compacted` 事件 + 投影应用——每步重投影自动返回压缩视图，压缩产物进入 provider 请求
3. **触发模型重划**：弃用 70/90/95 三档（已定）；改"低水位主动维护 + 压缩阈值（可配 / 可按模型覆盖）"
4. **安全网补洞**：P1-19 ①（窗口自适应，**保留**）、P1-23（read_file 帽，**保留**）；**P1-19 ②（单轮预算）已定暂不接线**（b3，回访条件见 §7.1 ②）

### 0.2 非目标（明确不做）

- 摘要质量（模板 A/B、去 wire cap、trim-retry、结束标记）→ **Phase 2**（**输入有界化已定不做**，2026-10-13——摘要输入走 Codex 路线：**移除既有 `content[:3000]`**（`compact.py:255`），全量喂入、超窗由 trim-retry 兜底）
- 去重打磨 / Pass 4 / 微压缩 / 主动剪枝 → **Phase 1 / 3**
- 工具参数截断 → **已定不采纳**
- UI read model 的压缩投影 → **已定：Phase 0 不动**（如产品需要，另排为独立需求）
- 撤销压缩（void / rollback）→ **已定：暂不做**，schema 预留

### 0.3 验收标准（可测）

1. `context_compacted` 事件写入通过 DAO 校验；投影三态正确（无 / 一次 / 多次压缩）
2. 触发链单测：水位门、rearm 门、未闭合调用保护、回收量门的通过与拒绝路径
3. 最小生产者在**投影形态消息**上生效：旧 tool 结果被替换；保护区不动；`<persisted-output>` 跳过；配对完整
4. 集成：压缩事件后，`_thinking_native` 构建的请求包含压缩视图（"第一步验证"断言的反转版）
5. flag=off 时全链路行为与现状一致（回归）
6. P1-19 ① / P1-23 修复有对应回归测试（② 不接线，无需测试）
7. **进程重启后仅凭事件流重建 → 压缩视图一致**（F11，核心价值验收）

---

## 1. 现状与断点（证据）

### 1.1 两条路径

```
【请求路径（rollout 模式）】
  agent_event 事件流 ──project_rollout_events (rollout.py:49)──▶ ChatCompletionHistoryProjection
      .messages = Tuple[ModelMessage, ...]   (human / ai(带 tool_calls) / tool(带 tool_call_id))
      .synthetic_tool_call_ids = frozenset   （缺结果的调用在投影中补 "aborted"，rollout.py:26）
          │
          ▼
  _thinking_native (tool_calling_agent.py:512-545)：provider 请求 = system + 投影消息 + 末条用户兜底

【压缩路径（现状）】
  _load_thinking_messages (base_agent.py:1330-1445)：system + historical_dialogues + memory
      └─ manage_context (base_agent.py:1438；唯一调用方)
             └─ L1 (compact.py:87-137) / L2 (:145-206) / L3 (:214-298)
                    ✗ 门控依赖 _is_observation_message（HUMAN + "Observation:" 前缀，:41-47）
                    ✗ round 以 Observation 闭合（:55-79）→ 投影消息只有 1 个 round
      └─ 结果只用于当轮 legacy 列表——rollout 分支在 _thinking_native 被绕过（只取 system + 用户兜底）
```

### 1.2 两个断点

- **断点 1（门控失配）**：`AgentMessage`（`agent.py:299-312`）**没有 `tool_calls` / `tool_call_id` 字段**——legacy 结构在类型层面就表达不了原生配对；L1/L2/L3 的判定函数在投影消息上全部不命中
- **断点 2（产物不落地 / 不入请求）**：事件类型封闭（`rollout.py:17`、`agent_event_db.py:35`、append 拒绝未知名 `:409-410`）；压缩结果无存放形态；请求由投影重建

### 1.3 可复用的既有资产（不要重复造）

| 资产 | 位置 |
|---|---|
| 投影器与合成占位（"aborted"） | `rollout.py:26,49-200` |
| turn 租约（`AgentTurnLease` + acquire/renew/release/assert） | `agent_event_db.py:62,225-304` |
| 事件序号 / 幂等 / payload 校验 | `agent_event_db.py:130-224,406-495` |
| per-result 落盘（已接线） | `tool_action.py:222` → `storage.maybe_persist` |
| `context.status` SSE 通道 | `agentic_data_api.py:3186-3195` |
| API 层预算配置加载（app TOML → `ContextBudgetConfig`） | `agentic_data_api.py:170-195` |
| L1 的 persisted-preview 跳过（**保留**） | `compact.py:117` |

---

## 2. 目标架构

### 2.1 三条原则

1. **投影是唯一 transcript 来源**；压缩 = **追加事件 + 投影应用**（Codex 式整份替换；D.16 已定）
2. **提交点 = 事件 append**（租约内、序号分配、原子）——append 失败即"什么都没发生"
3. **双轨分派按模式不按内容**：`enable_rollout_history`（或 loader 是否设置）决定走新链路或旧链路；旧链路原样保留

### 2.2 组件与职责

| 组件 | 位置（建议） | 职责 |
|---|---|---|
| 结构检测 `detect_turns` | 新文件 `agent/core/context/transcript_compaction.py` | ModelMessage → turns；边界不变量 |
| 保护区选择 `select_window` | 同上 | token 预算 + 轮数下限封顶 + **受保护 turn 排除**（未闭合调用 / 无主段 / 摘要标记） |
| 最小生产者 `build_pruned_replacement` | 同上（L1 逻辑 re-base） | 旧 tool 结果 → 截断（**输出格式见 §3.6**；`<persisted-output>` 原样跳过、幂等守卫）。事件层无独立快照路径字段，路径只存在于已落盘预览块自身 |
| 协调器 `maybe_compact` | **loader 包装内**（`agentic_data_api.py:3156-3158`，见 §2.3/F4） | 门控 + 组装 + 事件草稿 + 状态发射 |
| 事件写入 | 既有 `AgentRolloutSession.append` | 校验 + 租约 + 序号 |
| 投影应用 | `rollout.py` 新分支 | 见 §4.3 |

### 2.3 一次压缩的完整时序（建议，v2）

```
1. 挂载点（F4）：loader 包装 _load_rollout_projection（agentic_data_api.py:3156-3158——
   它已有 assert_turn_lease；在其后插入 maybe_compact，再 load）
   → 使【当前请求】就用上压缩视图；且正常路径只加载一次投影
   on_model_call（:3176-3177）保持原样，仅做 lease 断言
2. rollout_session.load_projection() → 投影对象（随带 latest_event_seq / last_compaction，见 §6）
3. 未闭合调用处理（F2）：synthetic_tool_call_ids 非空 → 【把含这些调用的 turn 标记为不可压缩】，
   其余干净区域照常压缩；全部 turn 都被保护 → no-op
   （不再整体跳过——否则含永久未决调用的会话永远无法压缩）
4. token 估算（投影消息，口径见 §9.5）→ 水位门：est ≥ max(effective_watermark, rearm_tokens)
   （effective_watermark = min(proactive_prune_tokens, window×0.4)，见 §5.1/F5）
5. select_window → 保护区（token 预算 + 轮数下限封顶；**不变量 I1：当前 turn 必在保护区**）
6. build_pruned_replacement 在副本上执行 → 回收量门：reclaimed ≥ min_reclaim_tokens
7. 通过 → append("context_compacted", payload)：整份 replacement_history + 记账 + rearm
   （租约内、序号分配、原子；失败 = 无变化）
8. 若已提交 → 重新 load（事件流已变）→ 返回压缩视图；发射 context.status（state 映射见 §2.5）
```

### 2.4 rollout 模式下 legacy 压缩的去向

- **跳过条件是 `context_compaction_enabled=true`**（协调器接管后，含 dry-run）：`_load_thinking_messages` 在该条件下于 rollout 模式下**不再调用 legacy `manage_context`**（避免无效计算与误导性状态）；**flag=off 时保持现状调用**——满足"flag 关 = 零行为变化"的硬验收（含 `context.status` 与 legacy 安全网）；legacy 路径（非 rollout）始终不变
- `context.status`：**enabled=true 时改由协调器按 §2.5 发射；enabled=false 时仍由 legacy 发射**（保持 UI 不退化）

### 2.5 `context.status` 的 state 新映射（F8）

绕过 legacy `manage_context` 后，warning / error / critical 三态失去定义；协调器改为：

| state | 含义 |
|---|---|
| `normal` | est < effective_watermark |
| `low_watermark_active` | effective_watermark ≤ est < 压缩阈值（低水位维护域） |
| `threshold_reached` | est ≥ 压缩阈值（Phase 2 摘要生产者触发域；Phase 0 仅状态标注） |
| `overflow` | provider 报超长（L4 响应式域） |

---

## 3. 设计 A：结构检测（turn 判定）

### 3.1 判定对象（已核实字段）

`ModelMessage`（`message.py:184-204`）：`role / content / tool_calls / tool_call_id / round_index`；`pass_to_model` 排除 `VIEW`（`:206-220`）。
投影产物（`rollout.py:97-160`）：`human`（普通文本）、`ai`（可带 `tool_calls`；content 可为 ""）、`tool`（带 `tool_call_id`）；缺结果 → 合成 `tool`（content="aborted"）。

### 3.2 turn 判定表

| 规则 | 定义 |
|---|---|
| **turn 起点** | 一条 `human` 消息（**排除**带"压缩摘要"元数据标记的） |
| **turn 内容** | 起点之后到下一个 `human` 之前的全部 `ai` / `tool` 消息（含连续工具循环、并行批次） |
| **system / view** | 不参与 turn；位置保持（system 恒在头部，不进压缩区） |
| **无主段** | 出现在任何 `human` 之前的 `ai` / `tool`（异常历史）→ 归入首段并**只保护不剪** |
| **尾部未闭合** | 结尾未遇到下一个 `human` 的 turn 保留为最后一个 turn |
| **不变量 I1（F10）** | 保护区必须覆盖**当前 turn**（最新 human 及其后全部消息）——防最新用户消息被压掉 |
| **不变量** | 边界只在 `human` 之前 → **永不切开 `ai(tool_calls)` ↔ `tool` 配对** |

### 3.3 边界用例

| # | 用例 | 期望 |
|---|---|---|
| 1 | 并行批次：1 条 ai（3 个 tool_calls）+ 3 条 tool | 同一 turn；不切开 |
| 2 | 连续工具轮：human → ai/工具 ×N → ai(最终) | 同一 turn（N 次工具循环） |
| 3 | 无工具问答：human → ai | 2 消息 turn |
| 4 | 中断轮：ai(tool_calls) 有合成 "aborted" | 该 turn **标记为受保护**（F2）；其余区域可压 |
| 5 | 摘要标记消息（未来 / 幂等） | 不作为 turn 起点；不被二次压缩 |
| 6 | 纯 system + human（新会话） | 1 个 turn；**无可压区**（no-op） |
| 7 | 历史以 tool 开头（异常） | 归入"无主段"，保护 |
| 8 | 空消息列表 | no-op |

### 3.4 保护区选择（建议）

```
从尾部向前累计估算 token，直到 ≥ protect_tail_tokens（默认 20000）
同时至少保护 protect_last_rounds（默认 3）个 turn
turn 数下限封顶 max_tail_rounds_floor（默认 8）——防"保护区被大输出堆满、无所可剪"死锁
【受保护 turn 一律不可压】：① 当前 turn（I1）② 含未闭合调用（synthetic id）的 turn（F2）
                        ③ 无主段 ④ 摘要标记所在段
可压区 = 全部 turn − 尾部保护区（token/轮数）− 受保护 turn
【逐 turn 排除，不要求连续前缀】受保护 turn 只是"不被修改"，原样保留在历史中；
其前后的干净旧 turn 照常可压（与 §2.3"其余干净区域照常压缩"一致）
```

### 3.5 伪代码

```python
def detect_turns(messages) -> list[Turn]:
    turns, current = [], None
    for i, m in enumerate(messages):
        if m.role in (SYSTEM, VIEW):
            continue
        if m.role == HUMAN and not is_summary(m):
            if current:
                turns.append(current)
            current = Turn(start=i, msgs=[i])
        else:
            if current is None:
                current = Turn(start=i, msgs=[i], orphan=True)   # 无主段
            else:
                current.msgs.append(i)
    if current:
        turns.append(current)
    return turns

def select_window(messages, cfg, protect_ids: frozenset[str]) -> Window:
    # 1) 由尾部回溯：token 预算 + 轮数下限 + 封顶
    # 2) 再叠加：含 protect_ids 的 turn、无主段、摘要段 → 全部剔除出可压区
    ...
```

### 3.6 生产者输出格式（F6）

- **已落盘结果**（content 含 `<persisted-output>`）：**原样保留**——路径指针在块内自带，不得替换（`compact.py:117` 的既有规则）
- **其他超门槛结果**：截断为
  `前 {N} 字符 … [truncated, total {total} chars]`
  —— **不带路径**：事件层无独立快照字段（`tool_result` payload 只有 `tool_call_id / name / content / success`，`tool_action.py:446-458`）
- **可选 Phase 1 小改**：`tool_result` payload 增加 `persisted_path` 字段（`ActionOutput.persisted_path` 已有数据），使生产者可补"全文在：{path}"
- 截断门槛：主动路径 `min_prune_chars=8000`；压缩内 200

---

## 4. 设计 B：`context_compacted` 事件

### 4.1 schema（payload 字段）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | int | ✅ | 从 1 起 |
| `reason` | str | ✅ | `proactive` / `threshold` / `manual` |
| `strategy` | str | ✅ | Phase 0 仅 `prune`；`summary` / `hybrid` 预留给 Phase 2 |
| `replacement_history` | list[dict] | ✅ | **整份替换历史**（顺序即最终 transcript 顺序）；元素 = `{role, content, tool_calls?, tool_call_id?}`，**role ∈ human / ai / tool——禁止 system（F3）**：system 由请求组装层单独注入（`tool_calling_agent.py:512-545`），进入 replacement 会造成双 system |
| `covers.through_event_seq` | int | ✅ | 该替换"已反映到"的事件序号（审计用；取自投影随带的 `latest_event_seq`，见 §6/F1） |
| `accounting` | dict | ✅ | `{tokens_before, tokens_after, reclaimed, pruned_count}`（**估算值**；口径 = 投影消息本体，不含 system / 工具 schema 开销——与门控同源，见 §9.5/F12） |
| `rearm_tokens` | int | ✅ | 下次允许压缩的水位（§6） |
| `created_by` | dict | ✅ | `{turn_id, ts}` |

示例（v2：去掉 system 条目）：

```json
{
  "schema_version": 1,
  "reason": "proactive",
  "strategy": "prune",
  "replacement_history": [
    {"role": "human", "content": "帮我分析订单表"},
    {"role": "ai", "content": "", "tool_calls": [
      {"id": "c1", "type": "function",
       "function": {"name": "sql_query", "arguments": "{…}"}}]},
    {"role": "tool", "tool_call_id": "c1",
     "content": "[sql_query] (25,000 chars result) … [truncated, total 25000 chars]"}
  ],
  "covers": {"through_event_seq": 87},
  "accounting": {"tokens_before": 52000, "tokens_after": 31000,
                 "reclaimed": 21000, "pruned_count": 6},
  "rearm_tokens": 79000,
  "created_by": {"turn_id": "…", "ts": "2026-10-13T12:00:00Z"}
}
```

### 4.2 校验规则（DAO `_validate_payload` 扩展，建议）

- `replacement_history` 非空 list；每元素 role ∈ **human / ai / tool**（**禁止 system**，F3）；`content` 为 str（有 `tool_calls` 时允许 ""）
- `tool_calls` 复用 `assistant_message` 的形状校验（id 非空且唯一、`function.name` 为 str、`arguments` 为 str）
- `tool_call_id` 非空；**replacement 内部配对必须闭合**（每个 `tool_call_id` 都有对应 ai 调用 id；未闭合 → 拒绝）——替代"投影端合成"
- `rearm_tokens ≥ 0`；`strategy` / `reason` 枚举校验
- 全部通过才 append
- **不变量断言（F10）**：I1 当前 turn 必在保护区（生产者不得触碰最新 human 及其后消息）；I2 replacement 不含 synthetic；I3 配对闭合（见上）；I4 无 system 角色

### 4.3 投影应用规则（`rollout.py`，建议）

```python
if event_type == "context_compacted":
    # 整份替换：丢弃此前累积的全部投影消息，替换为载荷
    projected = [ModelMessage(**m) for m in payload["replacement_history"]]
    pending_calls = {}          # 载荷已校验闭合；防御性再校验一次
    continue
```

- `_SUPPORTED_EVENT_TYPES` 增加 `context_compacted`（非 lifecycle 类型）
- 防御：载荷未闭合 / role 越界（system）/ content 非 str → `ValueError`（与其他畸形历史一致：不静默错配）
- `synthetic_tool_call_ids` 语义不变（只统计本投影中补齐的）
- 顺带计算（F1）：`latest_event_seq` = 本事件序号；`last_compaction` = `{rearm_tokens, through_event_seq, accounting}`

### 4.4 多次压缩语义

- **覆盖式**：后续 `context_compacted` 整体替换当前累积（含前一次压缩的产物）——与 Codex `history.replace(replacement_history)` 同语义
- **审计链**：每条的 `covers.through_event_seq` + `accounting` 记录"何时、压缩了什么、效果多少"
- **幂等**：同一事件重复投影结果一致（纯函数）

### 4.5 失败与回滚

- append 是唯一提交点：失败 → **无变化**（下一轮再试；退避/冷却属 Phase 1）
- **不做 un-compaction**：原始事件永存（可审计）；void 机制列入未决（§11）

### 4.6 版本与兼容

- `schema_version=1`；投影器对未知类型**已抛错**（`rollout.py:85-86`）→ **部署顺序必须"先代码（支持投影）后开启写入"**（flag 控制写入侧）
- **滚动发布风险（F7）**：多实例滚动期间，已写入 `context_compacted` 的事件流被**未升级的旧实例**投影 → `rollout.py:85-86` 直接抛错、旧实例会话全挂。处置二选一：
  - **a（推荐）部署纪律**：写入 flag 在所有实例升级完成前保持关闭；附检查清单（全实例版本确认 → 再开写入）
  - b 向前兼容改造：投影器对"未知类型"改为跳过 + warning——**契约变更**（牺牲"畸形即报错"的严格性），需单独评审
- 旧会话（无此事件）不受影响

---

## 5. 设计 C：触发模型与配置

### 5.1 新配置（建议名；进 `ContextBudgetConfig` + app TOML `agent_context` 段）

| 配置 | 默认 | 含义 |
|---|---|---|
| `context_compaction_enabled` | **false** | 总开关（灰度）；关 = 全链路与现状一致 |
| `context_compaction_dry_run` | **false** | 灰度第一档（§10.1 步 4）：全流程运行但**不提交**（不 append 事件、投影不变），输出结构化"将压缩什么"日志；仅 `enabled=true` 时有意义 |
| `proactive_prune_tokens` | **0**（关） | 低水位（绝对 token；参考值 ~48000，对齐 Hermes 示例）。**生效值按窗口夹紧（F5）**：`effective = min(config, window×0.4)`——否则 32k 窗口上永不触发、或触发一次后永不重新武装（rearm 超出窗口） |
| `compact_threshold_ratio` | **0.75** | 压缩阈值（替代 70/90/95）；**可按模型覆盖**。⚠️ Phase 0 仅状态标注用途（**F9：死配置**），摘要生产者 Phase 2 接入后才消费 |
| `compact_threshold_tokens` | 0 | 可选绝对值覆盖（0 = 不用） |
| `min_reclaim_tokens` | **4096** | 回收量门（缓存契约，按"有缓存"处理——已定） |
| `protect_tail_tokens` | 20000 | 保护区 token 预算（估算） |
| `protect_last_rounds` | 3 | 保护区轮数下限 |
| `max_tail_rounds_floor` | 8 | 轮数下限封顶（防死锁） |
| `min_prune_chars` | 8000 | 主动路径剪枝门槛（压缩内 200） |

### 5.2 触发决策（伪代码，v2）

```python
est = estimate_tokens(projection.messages)
if not enabled or proactive_prune_tokens <= 0:
    return None
watermark = min(proactive_prune_tokens, window_tokens * 0.4)        # F5：窗口夹紧
rearm_gate = projection.last_compaction.rearm_tokens if projection.last_compaction else 0
if est < max(watermark, rearm_gate):                                # 水位门 + rearm 门
    return None
protected_ids = projection.synthetic_tool_call_ids                  # F2：改为"保护"，不再整体跳过
window = select_window(projection.messages, cfg, protect_ids=protected_ids)
if window.empty:                                                    # 全部 turn 被保护 → no-op
    return None
replacement, stats = build_pruned_replacement(projection.messages, window, cfg)
if stats.reclaimed < min_reclaim_tokens:                            # 回收量门
    return None
rearm = stats.tokens_after + max(stats.reclaimed, watermark, min_reclaim_tokens)
return CompactionDraft(replacement, rearm,
                       through_event_seq=projection.latest_event_seq)  # F1
```

### 5.3 迁移映射（旧 → 新）

| 旧字段 | 处置 |
|---|---|
| `warning_threshold` (0.70) | rollout 模式不再使用（legacy 保留）；协调器不消费 |
| `error_threshold` (0.90) | 同上；压缩触发改由 `compact_threshold_ratio`（Phase 2 接摘要生产者时消费） |
| `critical_threshold` (0.95) | 保留给 L4 响应式兜底（legacy 路径） |
| `max_observation_age_rounds` / `truncated_observation_max_chars` | legacy 专用；新链路用 turn 判定 + `min_prune_chars` |

---

## 6. 设计 D：rearm 水位存放（决策）

### 6.1 方案对比

| | 事件流（`payload.rearm_tokens`） | 会话配置（chat_history 加列 / ext_info） |
|---|---|---|
| 权威性 | 单一来源，与压缩事件原子同写 | 第二来源，可能不一致（D.13 教训） |
| schema 变更 | 无（复用新事件） | 需要 chat_history 迁移 |
| 读取成本 | 投影随带（见 6.2/F1） | 一次额外查询 |
| 崩溃恢复 | 天然（事件已排序） | 需处理写入时序 |

### 6.2 决策（建议；v2 修正读取方式）

**放事件流**：`rearm_tokens` 写进 `context_compacted.payload`；**读取 = 投影对象随带的 `last_compaction`（F1）**——由投影器遍历事件时顺带计算，不新增查询：

```python
@dataclass(frozen=True)
class CompactionState:
    rearm_tokens: int
    through_event_seq: int
    accounting: dict

# ChatCompletionHistoryProjection 扩展字段（rollout.py:42-46）：
#   latest_event_seq: int | None
#   last_compaction: CompactionState | None
```

**v1 → v2 修正说明**：v1 曾写"协调器本就要 load 事件列表（零额外查询）"——**该假设错误**：`load_projection()` 只返回投影结果（`messages` + `synthetic_tool_call_ids`），不含事件序号与压缩状态。故必须扩展投影对象（投影器本就遍历全部事件，计算零成本）。

### 6.3 并发

读取在租约内；水位仅"提示性"（抑制频繁改写），不承担跨进程强一致职责。

---

## 7. 设计 E：接线修复（P1-19 / P1-23）

### 7.1 P1-19（两个函数接线）

- **① 窗口自适应**：在两处 `ContextBudgetConfig` 构造点（`agentic_data_api.py:_load_context_budget_config`、`base_agent.init_context_management` 的 fallback 分支 `:87-96`）用 `tool_result_budget_for_context_window(max_context_tokens)` 派生 `tool_result_threshold` 与 `turn_result_budget` —— 修复小窗口 #23767 场景
- **② 单轮聚合预算（2026-10-13 已定：b3——先不接线，观察）**：
  代码现场（已核实）：`enforce_turn_budget` 是 `ToolResultStorage` 的方法（`storage.py:382`，`results: List[(content, name, call_id)] + budget → 替换后列表`），**全仓零调用**；但执行器现状是【**逐 call 完成即落盘**】：单工具分支 `tool_action.py:342`，并行分支的每个 worker `_run_one` 完成后立即 `:376` 落盘——**不存在"整批聚合后再落盘"的天然时点**，而事件 append-only（落盘后不可改写）。
  因此"批次聚合后（落盘前）调用"需要改造执行器顺序、与既有持久化属性冲突。候选：

  | 选项 | 做法 | 代价 |
  |---|---|---|
  | a | 把 `_persist_tool_result` 移到 `gather` 之后统一执行 → `enforce_turn_budget` → 按 spec 顺序逐个落盘 | 并行批次"完成即落盘"→"整批后落盘"：**崩溃窗口变大**（已完成工具的结果要等批次结束才落盘），与 D.7/D.14 刻意设计的恢复边界冲突；换来确定性与"挑最大者"完整语义 |
  | b1 | 保持完成即落盘；在每次工具返回后、落盘前做**增量预算检查**（累计超预算则落当前条） | 无耐久性退化；"挑最大者"退化为"先到先判"；次序相关（非确定） |
  | b2 | 不改执行器：把"回合预算"做成**投影期规则**（构建请求时把超预算回合的最大结果替换为预览；全文在事件中，另写盘供读回） | 保耐久 + 保精确；属新机制（读路径写盘副作用），范围大于"接线"，宜 Phase 1/2 |
  | b3 | 先不接线（观察） | 叠爆未实测前零成本；Layer 2 已覆盖单条大结果 |
  - **决策（2026-10-13）：b3——先不接线，观察**。Layer 2 已覆盖单条大结果；"多条中等结果叠爆"未实测前零成本。**回访条件**：观测到"单步增量跳跃"（一次并行批次让 est 跳过水位/保护区阈值、压缩被动跟随）→ 回到 b1（首选）或 b2
  - 补充核实：`budget` 默认取 `self._config.turn_budget`（`storage.py:398-400`）；若未来接线，配置传递点为构造处 `base_agent.py:138-143`（当前未传 config，走默认）

### 7.2 P1-23（read_file 帽）

- schema：`limit` 增加上界（Hermes 对齐：**maximum = 2000**）
- 内部：单次输出的**字符帽（~100k）**；超帽截断并提示 `offset=...` 续读（现有续读提示见 `read_file.py:141-145`）
- 回归测试：大文件 + 大 limit → 输出有界、续读可用

---

## 8. 改动清单

| 文件 | 动作 |
|---|---|
| `agent/core/rollout.py` | + `context_compacted` 到 `_SUPPORTED_EVENT_TYPES`；+ 替换分支；+ 载荷校验/转换；**+ 投影类扩展 `latest_event_seq` / `last_compaction`（F1）** |
| `dbgpt_serve/agent/db/agent_event_db.py` | + 类型到 `_EVENT_TYPES`；+ `_validate_payload` 分支 |
| `agent/core/context/compact.py` | **本期不改**（legacy 路径与函数原样保留）；新模块参考其 L1 既有行为（跳过 `<persisted-output>`、截断文案）——结构检测在 `transcript_compaction.py` 实现 |
| `agent/core/context/transcript_compaction.py`（新） | `detect_turns` / `select_window`（含受保护 turn） / `build_pruned_replacement` / `estimate_tokens`（纯函数） |
| `agent/core/context/budget.py` | + 新配置字段（§5.1）+ 迁移默认 |
| `openapi/api_v1/agentic_data_api.py` | 扩展 `_load_context_budget_config`；**在 `_load_rollout_projection` 包装内接入 `maybe_compact`（F4；`on_model_call` 仅保持 lease 断言）**；状态发射 |
| `agent/core/base_agent.py` | rollout 模式跳过 legacy `manage_context`（`_load_thinking_messages`）；P1-19 ① 的 fallback 接线（**保留**） |
| `agent/expand/actions/tool_action.py` | ~~P1-19 ②：批次 `enforce_turn_budget`~~ → **已定暂不接线**（b3；未来若启用，接线点与 b1/b2 选型见 §7.1 ②） |
| `openapi/api_v1/tools/read_file.py` | P1-23：limit 上界 + 字符帽 |
| 测试 | **步骤 1**：扩展 `core/tests/test_rollout.py`、`serve/…/db/tests/test_agent_event_db.py`（细则见实施交接 Brief §3.C）；后续步骤按需新增 `test_transcript_compaction.py`、扩展 app 层 lifecycle 测试 |

---

## 9. 测试与验收计划

### 9.1 单测

- 结构检测：§3.3 八个边界用例
- 投影应用：无 / 一次 / 多次压缩；畸形载荷拒绝；`latest_event_seq` / `last_compaction` 计算
- 触发链：四条门（水位 / rearm / 未闭合保护 / 回收量）各自的通过与拒绝
- 生产者：截断正确、保护区不动、persisted-preview 跳过、幂等

### 9.2 集成

- 事件流 → 协调器 → append → 重投影 → `_thinking_native` 请求包含压缩视图（**"第一步验证"断言的反转版**）
- 失败注入：append 抛错 → 转写不变、下一轮可重试
- **进程重启后投影（F11，核心价值）**：新进程仅凭事件流重建 → 压缩视图一致（含 `last_compaction` 读取）
- **lease 丢失（F11）**：append 时租约已被夺走（`TurnLeaseLost`）→ 无变化、不写坏事件流
- **mid-turn 连续两次压缩（F11）**：第二次覆盖第一次（§4.4 语义 + 审计链完整）
- **未闭合调用会话（F2）**：含 synthetic 的 turn 被保护，其余区域可压缩

### 9.3 回归

- flag=off：行为与现状一致
- legacy 链路（非 rollout）不受影响

### 9.4 运行时验收（灰度开启后观察）

压缩频率（每小时次数）、单次回收量分布、请求大小分布、重复工具调用率、SSE 状态正常。

### 9.5 估算口径（F12；2026-10-13 已定）

**决策：协调器使用独立 chars/4 快估算**（门控与记账同源同口径）：

```
tokens ≈ ceil(Σ len(content) / 4) + Σ ceil(len(json.dumps(tool_calls)) / 4)
范围：投影消息本体（不含 system prompt 与工具 schema 开销）
```

- 门控、`accounting`、`context.status`（§2.5）**全部使用这一个函数**；口径不一致会导致"门控触发但记账为负"的误读
- 快：每步都要算（含 MB 级 content），纯长度 O(n)；与 legacy `budget.py` 的 tokenizer 计数**并存但互不干预**（legacy 路径保持现状）
- 已知误差（偏保守方向）：CJK 文本 1 字符≈1 token 时会被低估——如需精确可后补 CJK 修正（对齐 Hermes），Phase 0 先用纯 chars/4

---

## 10. 实施顺序、灰度与风险

### 10.1 顺序

1. 投影 / 事件层（schema + 应用 + 校验 + 投影类扩展 + 单测）——不改变行为（尚无生产者）
2. P1-19 ① / P1-23 接线修复（独立可发布；**P1-19 ② 已定不接线，见 §7.1 ②**）
3. 协调器 + 最小生产者 + 触发（**flag 默认关**）
4. 灰度：先"只观测不提交"（dry-run 记录将压缩什么）→ 小流量提交 → 默认

### 10.2 风险

| 风险 | 缓解 |
|---|---|
| 投影与类型扩展不同步 → 旧进程读到新事件抛错 | 部署顺序（先代码后开启写入）+ flag；**滚动发布纪律见 §4.6/F7（全实例升级完成前写入保持关闭）** |
| 压缩后 UI / 旧历史视图不一致 | **已定：Phase 0 不动 UI**；压缩仅影响模型 transcript |
| 估算误差导致门控抖动 | 保守估算 + `accounting` 可观测 + 门槛可调（口径统一见 §9.5） |
| 事件体积（每次存整份替换） | 体积 = 替换后历史（小于被替换内容）；后续可演进范围引用式 |
| 与 GptsMessages 回填重新冲突 | rollout 模式已关闭回填（D.13）；保持 |

---

## 11. 决策记录（2026-10-13 全部拍板）

| # | 事项 | 决策 | 落点 |
|---|---|---|---|
| 1 | **UI read model** | Phase 0 不动 UI（压缩只影响模型 transcript）；如产品需要"UI 也显示压缩后视图"，另排为独立需求 | §0.2 |
| 2 | **void（撤销压缩）** | 暂不做，schema 预留（`schema_version`）；遇到真实运维场景再实现 | §0.2、§4.5 |
| 3 | **估算器** | **独立 chars/4 快估算**（门控 / 记账 / 状态同源同口径） | §9.5 |
| 4 | **投影加载次数** | 单次 load 即可（F4 设计，触发时重载一次）；接入更多消费者时再评估缓存 | — |
| 5 | **P1-19 ② 接线点** | **b3：先不接线，观察**；回访条件 = 出现"单步增量跳跃" → 回到 b1（首选）或 b2 | §7.1 ② |
| 6 | **向前兼容（F7-b）** | **方案 a：部署纪律**（全实例升级完成前写入保持关闭）；保持"畸形即报错"契约，不采用"未知类型跳过" | §4.6 |

---

> **结论**：Phase 0 的最小闭环 = **turn 结构检测 + `context_compacted` 事件（整份替换）+ 投影应用（含投影类扩展）+ 一个最小机械生产者（L1 re-base）+ 新触发配置 + 两处接线修复**，全部走 flag 灰度。机制层（事件 / 投影）可独立先行且零行为变化；生产者与触发最后接入。
