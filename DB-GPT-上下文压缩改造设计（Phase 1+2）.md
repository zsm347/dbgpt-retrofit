# DB-GPT 上下文压缩改造设计 —— Phase 1 + Phase 2（合并批）

> **范围**：Phase 1（无损/廉价层：去重、泄压阀、剪枝形态）+ Phase 2（权威化：摘要生产者、无硬帽、trim-retry、结束标记、模板三版本）。一批实施、分 6 组提交（§7）。
>
> **前置**：Phase 0（机制层）已完成并验收（提交 `cfe92ca17` / `fdcc0d0a2` / `266025206`，见《DB-GPT-上下文压缩改造设计（Phase 0）.md》与验收记录）。本设计是**在其之上的增量**：不改事件 schema、不改投影替换语义、不改协调器挂载点，只加"更聪明的生产者 + 摘要生产者 + 异步结构"。
>
> **事实依据**：Hermes `agent/context_compressor.py`、Codex `codex-rs/core/src/compact.rs` 对照；本设计凡涉代码均为一手核实（带 `file:line`），设计选择带理由。
>
> **2026-10-14 已定（与用户确认）**：Phase 1 三项范围；与 Phase 2 合并为一批；剪枝形态 = **激进一行**（不落盘、不建 per-tool 模板；详情找回 = **重跑同一调用**，参数仍在 transcript）。
>
> **v2 修订（2026-10-14 自评审）**：F1 去重"破引用"修正（保留副本可剪 → 不去重）；F2 新增 `select_summary_region`（连续前缀段、吸收旧摘要）；F3 `CompactionWindow.tail_tokens`（泄压阀数据来源）；F4 trim-retry 上限语义；F5 组 4/5 同批约束；F6 摘要延迟/超时/lease；F7 无帽按后端显式处理；F8 `tool_success` 不清理 + persisted 不参与去重；F9 净收益门措辞 + `min_prune_chars` 观测项。
>
> **实施交接 Brief**：《[DB-GPT-实施交接Brief（Phase 1+2）](./DB-GPT-实施交接Brief（Phase 1+2）.md)》——自包含施工任务书（6 组提交 / 基线红线 / 环境命令 / 验收三态 / 报告模板）。

---

## 0. 范围与验收

### 0.1 目标（六项）

| 组 | # | 内容 |
|---|---|---|
| P1 | ① | **去重**：内容指纹，同内容保最新一份，旧副本替换为引用行（无损） |
| P1 | ② | **泄压阀（Pass 4）**：保护区自身超软预算时允许进区降级，防 #61932 死锁 |
| P1 | ③ | **剪枝形态**：激进一行（`args` 预览 + `success` + 大小）+ 净收益门 + 兼容旧标记 |
| P2 | ⑦ | **摘要生产者**：事件化提交（`strategy="summary"`）+ token 重算 + lease + 旧摘要过滤 |
| P2 | ⑧ | **无 wire cap**（纠错）+ **trim-retry**（超窗降级）+ **结束标记** |
| P2 | ⑫ | **模板三版本 + 开关**（sections6 / handoff / hybrid） |

### 0.2 非目标（明确不做）

- Phase 3（主动剪枝低水位、微压缩、服务端压缩/换窗档）
- **模板 A/B 的评估活动**（本批只实现 3 个模板 + 开关；真实模型对比评分另行安排）
- **legacy 路径（非 rollout）不改**——含 `compact.py` 的 `content[:3000]` 与 `max_new_tokens=2000`（见 §9-D5）
- **per-tool 信号提取模板**（不建；exit code/匹配数等详情靠重跑获取）
- **剪枝时强制落盘**（不做；`<persisted-output>` 预览块自身的路径照旧保留）

### 0.3 验收标准（可测）

1. §6 全部单测通过；既有测试无新增失败
2. `flag=off`：与改动前逐字节一致（回归）
3. `dry-run`：P1 变更照常试算（回收量/门控分布可观测）；**P2 只记"将会摘要"、不调 LLM、不写事件**（§3.9）
4. `flag=on`：剪枝为一行格式（含 `success` 信号）；摘要路径：事件写入、投影中段摘要、**进程重启后重建一致**
5. trim-retry：mock 超窗错误 → 逐条裁剪重试 → 成功 / 有界放弃（各一例）
6. 模板开关三值可切换（单测断言 prompt 选择正确）
7. **无 wire cap（F7）**：按**实际部署形态**断言——远程 provider：捕获出站请求，断言**不显式设置** `max_tokens` 上限；本地模型后端（mlx/vllm/hf 有 2048/4096 默认帽）：记录默认并**显式覆盖为不设上限**（§3.5）
8. 兼容幂等：Phase 0 旧截断痕迹（`… [truncated, total N chars]`）与去重引用行不被二次处理

---

## 1. 前提回顾（Phase 0 现状，一句话）

`context_compacted` 事件 + 投影整体替换 + 投影类扩展（`latest_event_seq`/`last_compaction`）+ 协调器（loader 包装、flag 门控、dry-run、append 唯一提交点）+ 剪枝生产者（`transcript_compaction.py`：turn 判定 / 保护区 / 截断）+ 触发链（水位夹紧 / rearm / 回收量门）。**Phase 0 的剪枝替换文本是"前 8000 字符 + 标记"，本设计把它升级为一行形态。**

---

## 2. Phase 1 设计

### 2.1 ① 去重 pass（无损）

**位置**：新增纯函数 `dedupe_replacement(...)`，由协调器**先去重、后剪枝**（去重产出的引用行很短，不会再进剪枝）。

**算法**：
```
候选：tool 消息，len(content) ≥ config.dedup_min_chars（默认 200），
      且 content 不含任何"已处理标记"（§2.3 的三个正则）；
      【persisted 块不参与】（F8：保持"指针"语义简单——两个副本指向的文件可能不同）
指纹：md5(content.encode("utf-8"))
保留：从【最新】向最老扫描，同指纹保留最新一份全文
生效条件（F1，防"破引用"）：仅当【保留副本不会被剪】时，才把更老的副本
      （落在 window.compressible_indices 内）替换为引用行；
      若保留副本会被剪（∈ 可压区 ∧ len > min_prune_chars）→ 不生成引用行，
      交给剪枝把两份都剪成一行（无引用、无谎言）
"不会被剪" = 保留副本在保护区 / len ≤ min_prune_chars / 是 persisted 块
引用行：[duplicate of the later {tool_name} result — content omitted]
      （前缀 [duplicate of the later  供幂等识别；tool_name 缺失时用 "tool"）
边界：保护区内的副本不动；"保留的那份"可以在保护区内（内容仍可达，无损）
```

**无损性**：被替换内容与保留副本逐字节相同（同指纹）——不丢信息。
**参数**：`dedup_min_chars`（默认 200，避免小结果的引用行反而更长）。
**已知残留（§8-8）**：保留副本在"日后"老化进可压区被剪时，早先的引用行会变成无内容可指的短行——影响轻微（模型按"详情=重跑"处理），先记录不修。

**测试**：§6.1（含 F1 的"保留副本可剪 → 不去重"用例）。

### 2.2 ② 泄压阀（Pass 4，防 #61932 死锁）

**触发**（在 `decide_compaction` 内、正常窗口之后）：
```
tail_tokens > int(config.protect_tail_tokens × config.pressure_soft_factor)   # 默认 1.5
且（window.empty 或 stats.reclaimed < config.min_reclaim_tokens）
```

**`tail_tokens` 的数据来源（F3）**：尾部保护区各 turn 的估计 token 和——由 `select_window` 计算并随 `CompactionWindow` **新增字段 `tail_tokens: int`** 返回（当前该类只有 `turns` + `compressible_indices`，已核实 `transcript_compaction.py:60-68`）。

**降级顺序**（Hermes 对齐，`context_compressor.py:2990-3050`）：

| 阶段 | 动作 |
|---|---|
| A | 保护区内、**当前 turn 之外**的大工具结果 → 一行（从最老开始，够用即停） |
| B | 仍超 → 除**最新一条工具结果**外，保护区内所有工具结果降级 |
| C | 仍超 → **最新一条工具结果也降级**（绝对最后手段） |

**硬底线与偏差**：
- **活跃用户消息（当前 turn 的人类消息）永不降级**（I1 不变量不破）
- `<persisted-output>` 预览块**仍然跳过**（偏差说明：Hermes 无此保护；我们保留指针不换空间）
- 未闭合调用 turn 仍保护（**已知局限**：若死锁恰在此类 turn 内，泄压不生效——记录不修）
- pressure 阶段算出的 reclaim 仍须过 `min_reclaim_tokens`；审计/日志记 `gate="pressure"`

**实现**：`select_window(..., pressure: bool = False)` 增加 pressure 分支（并计算 `tail_tokens`）；`decide_compaction` 顺序 = 正常窗口 → pressure 窗口 → 拒绝。
**测试**：§6.2。

### 2.3 ③ 剪枝形态（激进一行 + 信号 + 净收益）

**新替换格式**：
```
[{tool_name}] args≈{arguments 预览 ≤ prune_args_preview_chars(默认80)，换行→空格} · success={true|false|?} · {N} chars
```

**数据来源**：

| 字段 | 来源 | 缺失时 |
|---|---|---|
| `tool_name` / `args` | 同 turn 内 ai 消息的 `tool_calls`（遍历时构建 `tool_call_id → (name, arguments)` 映射） | `[tool] args≈?` |
| `success` | 投影新增映射（§2.4） | `?` |
| `N` | `len(content)` | — |

**净收益门（F9 措辞修正）**——双保险；注意：一行形态下 Phase 0 的 **8KB 边缘微负收益本就不复存在**（8000 字符 → ~150 字符，净省 ~7850）：
```
len(original) - len(new_line) ≥ config.min_saving_chars    # 默认 500
```

**幂等与兼容**（三个标记都识别为"已处理，跳过"）：
```
新格式：  ^\s*\[[^\]]+\]\s+args≈.*·\s+success=(true|false|\?)\s+·\s+\d+ chars\s*$
Phase 0： … \[truncated, total \d+ chars\]\s*$        （沿用现正则 _TRUNCATION_MARKER_RE）
引用行：  ^\[duplicate of the later 
```

**保持不变**：`<persisted-output>` 原样跳过；`min_prune_chars`（8000）仍是"是否够格被剪"的门槛；合成条目（synthetic）仍被剔除（投影器重推导）。
**参数**：`min_saving_chars`、`prune_args_preview_chars`（§4）。
**测试**：§6.3。

### 2.4 投影扩展：`tool_success` 映射

- `ChatCompletionHistoryProjection` 新增 `tool_success: Mapping[str, bool] = field(default_factory=dict)`（frozen dataclass，与 `accounting` 同法）
- 投影器 `tool_result` 分支：读 `payload.get("success")`，`isinstance(..., bool)` 才记录 `tool_success[call_id] = value`
- **replacement 载荷不携带 success**（事件 schema 不变）；替换后来源缺失 → 生产者渲染 `?`（已被一行化的内容本来也不会再剪）
- **replacement 后不清理映射（F8）**：替换保留原 call id（内容被改写、id 不变）→ 既有条目仍有效；被摘要移除的 id 残留无害
- 安全性：纯投影期信号，不进协议、不影响请求组装

---

## 3. Phase 2 设计

### 3.1 架构：异步结构与挂载（核心）

**现状**（已验收）：
```
_load_rollout_projection():  assert_turn_lease → run_in_threadpool(maybe_compact) → status → return projection
```

**改为**：
```
_load_rollout_projection():  assert_turn_lease
    → outcome = await maybe_compact_async(session, config, llm_client=..., model_name=...,
                                           window_tokens=..., dry_run=...)
    → 发射 status → return outcome.projection or load_projection()
```

**`maybe_compact_async`**（`rollout_compaction.py` 新增；保留原同步 `maybe_compact` 作为"仅剪枝"内核与兼容测试）：
1. （threadpool）load projection + 决策：`decide_compaction`（剪枝，含 pressure）+ 新增 `decide_summary`（§3.2）
2. 策略选择：**summary 优先**（`est` 达阈值）；否则 prune；否则 no-op
3. summary 路径：（threadpool）组装输入 →（async）`await` LLM（trim-retry 循环）→（threadpool）构建 replacement + append + 重载
4. **摘要任何失败 → 回退尝试 prune**（便宜兜底）→ 仍不行 → no-op；**never-raise 契约不变**
5. 依赖注入：`llm_client` 与 `model_name` 从 loader 包装闭包传入（都已可达：`agentic_data_api.py` 的 `_react_agent_stream_impl`）
6. **延迟与超时（F6）**：摘要调用发生在 **loader 包装内 = 请求关键路径**，达阈值时每步最多一次 LLM 调用（额外延迟；已核实客户端无超时参数，依赖下层 provider）；失败/超时 → 回退剪枝（请求继续）；**append 时 lease 失效 → no-op**（既有机制）；"摘要调用时长"列入观测项（§8）

### 3.2 触发与策略选择（summary）

```
threshold = compact_threshold_tokens_for(config, window)          # 已有函数（ratio 或绝对值）
门 1：est ≥ threshold
门 2：est ≥ max(effective_watermark, rearm_gate)                  # rearm 仍防频繁改写
门 3（预回收，摘要长度未知 → 用假设值）：
      region_tokens − assumed_summary_tokens ≥ min_reclaim_tokens
      assumed = int(region_tokens × 0.20)     # Hermes 预算比例；仅用于门控与 dry-run 估算
生成后：真值记账；若摘要为空、或替换后不短于原区域 → 不 append（no-op）
```

（`region_tokens` = 摘要区域（§3.3）的估计 token 和，由 `select_summary_region` 顺带给出。）

### 3.3 摘要输入组装（新文件 `transcript_summary.py`）

- **区域（F2，替代 v1 的"复用可压区"）**：新增 **`select_summary_region`**——取【从最老消息起、**连续**的可压前缀段】，遇**结构保护**（未闭合调用 turn / 无主段 / 当前 turn / 尾部保护区）即停止；**旧摘要段不阻断**（其文本进入输入、随区块一起被吸收——迭代更新，避免摘要堆叠）。
  ⚠️ 原因：可压区是"**逐 turn 排除**"的集合、可被受保护 turn 穿插；而摘要替换是**单一连续 splice**（`messages[:start] + [summary] + messages[end:]`），非连续集合做不到。
  区域内已被一行化/去重的消息照常进入输入（文本已在历史里）。
- **序列化**（对齐 L3 格式但**不截断**）：
  ```
  [{role}]: {content}                      # 每条消息一行；content 全量（无 [:3000]）
  [tool_calls] {name}({arguments})         # ai 带 tool_calls 时追加一行；arguments 全量
  ```
- **提示词**：模板（§3.7）+ 序列化文本；**单条 user 消息**发送（与 L3 同形态）
- **不做输入有界化**（已定）；超窗由 trim-retry 兜底
- **trim-retry（超窗降级）**：
  - 仓库**无现成错误分类**（已核实）→ `_is_context_overflow_error(exc)` 按文本子串匹配：
    `context length` / `context_length_exceeded` / `maximum context` / `too many tokens` / `prompt is too long` / `reduce the length` / `exceeds the maximum` / `413`
  - 每次超窗：从输入**最老的 turn** 裁掉一个（turn 对齐、保配对）→ 重试
  - **上限（F4，消歧）**：裁剪循环的终止条件 = **只剩 1 个 turn**（放弃，no-op）；`summary_trim_max_attempts`（默认 8）**只约束"无进展的重试"次数**（有进展则该计数清零——Codex 语义）
  - 非超窗失败：不裁剪，直接走失败路径（计数 + 冷却，§3.6）
  - 日志记错误原文前 200 字符（供后续把未命中的模式补进列表）

### 3.4 摘要消息形态与位置

- **消息**：`role=human`，内容：
  ```
  [Context Summary of {N} earlier messages]

  {summary_text}

  --- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
  ```
  - 前缀与 L3（`compact.py`）**完全一致** → `SUMMARY_MARKER` / `is_summary_message` 无需改动
  - 结束标记对齐 Hermes `_SUMMARY_END_MARKER`（`context_compressor.py:238`）
- **位置**：**中段原位替换**
  ```
  replacement = messages[:region_start] + [summary_msg] + messages[region_end:]
  ```
- **旧摘要过滤（F2 同步）**：`select_summary_region` 的区域内，旧摘要文本进入新摘要输入并**随区块一起被吸收**（迭代更新、不堆叠）；区域外（结构保护之后 / 保护区）的旧摘要保留原文。注：**prune 路径**对"摘要标记段"的保护不变（`select_window` 语义不动）

### 3.5 去 wire cap（纠错）

- 生产者**不复用 `generate_text`**——它在失败时吞异常返回空串（`llm_client.py:215-223`），无法支撑 trim-retry
- 改直调：`llm_client.create(messages=[{"role":"user","content":prompt}], llm_model=..., temperature=..., stream_out=False, conv_id=...)`（已核实 `create(verbose=False, **config)` 为透传签名，直调可行）
- **"不传"不够（F7，已核实）**：本地模型后端 `params.get("max_new_tokens", 2048/4096)`（mlx/vllm/hf）——不传会**落到默认帽**；远程 provider 路径不传通常无上限；`model/cluster/base.py:21` 显示 `None` 为合法值
  → 实现要求：**显式传 `None` 并确保透传（None = 不设上限）**，或按后端分类处理；验收按**实际部署形态**断言（§0.3-7）

### 3.6 失败处理与冷却（新增）

- 会话级内存计数（**非持久化**，重启重置——接受的取舍）：连续 `summary_failure_threshold`（默认 3）次失败 → 冷却 `summary_cooldown_seconds`（默认 60）内不再尝试摘要（**剪枝不受影响**）
- 成功 → 计数清零
- 理由：摘要失败（provider 抖动/超窗耗尽）不得每步重试（防重试风暴，对齐 Hermes 失败冷却租约）

### 3.7 模板三版本 + 开关

- 配置 `summary_template: "sections6" | "handoff" | "hybrid"`（默认 **hybrid**；A/B 评估时切换）
- 三份模板文本写入代码常量（`transcript_summary.py`）：

```
sections6（= 现有 L3 六段，compact.py 原文）：
  1. Original Task / 2. Completed Steps / 3. Current State
  4. Key Data（重要值、路径、变量名——必须精确）/ 5. Errors Encountered / 6. Next Steps

handoff（Codex 四段，对齐 "为另一个即将接手的 LLM 写交接摘要"）：
  Progress + key decisions / Context, constraints, or user preferences /
  What remains / Critical data（精确值）

hybrid（默认；③ 合体第三版）：
  六段骨架 + 新增 "User Constraints & Preferences" 槽 +
  handoff framing（"another LLM will resume"）+ 收尾声明
  （"已完成的工作勿重做；仅从 Next Steps 继续"）
```

### 3.8 事件提交与记账

- payload：`reason="threshold"`、`strategy="summary"`、完整 `replacement_history`、`covers.through_event_seq`、
  `rearm_tokens = tokens_after + max(reclaimed, effective_watermark, min_reclaim_tokens)`、
  `accounting = {tokens_before, tokens_after, reclaimed, summary_chars}`、`created_by`
- token 重算：`estimate_tokens`（与门控**同源同口径**，Phase 0 §9.5 不变）
- lease：append 走既有租约路径；失败 → no-op（append 仍是唯一提交点）

### 3.9 dry-run 语义（P2 特有）

- dry-run 下 summary 路径**不调 LLM、不写事件**；日志记：
  `event="dry_run"`、`strategy="summary"`、`gate`、`est_tokens`、`threshold`、`region_tokens`、`assumed_reclaim(=region×0.8)`、`would_template`
- 理由：dry-run 承诺零副作用（含零 LLM 开销）；摘要质量不进 dry-run 观测范围（由独立评估活动覆盖）

---

## 4. 配置新增（默认全关/保守）

| 字段 | 默认 | 用途 |
|---|---|---|
| `min_saving_chars` | 500 | ④ 剪枝净收益门（§2.3） |
| `prune_args_preview_chars` | 80 | 一行里的 args 预览长度 |
| `dedup_min_chars` | 200 | 参与去重的最小内容长度（§2.1） |
| `pressure_soft_factor` | 1.5 | 泄压阀触发倍数（§2.2） |
| `summary_template` | `"hybrid"` | 模板选择（§3.7） |
| `summary_trim_max_attempts` | 8 | trim-retry 的"无进展重试"上限（§3.3） |
| `summary_failure_threshold` | 3 | 连续失败进冷却的门槛（§3.6） |
| `summary_cooldown_seconds` | 60 | 冷却时长（§3.6） |
| （复用）`compact_threshold_tokens` / `compact_threshold_ratio` / `min_reclaim_tokens` / `protect_tail_tokens` / `protect_last_rounds` / `max_tail_rounds_floor` / `min_prune_chars` | — | Phase 0 已配 |

> **观测项（F9）**：一行形态下 `min_prune_chars`（8000）本可下调——用 dry-run 的回收量分布决定，**不在本批变更**。

---

## 5. 改动清单

| 文件 | 动作 | 对应 |
|---|---|---|
| `core/context/transcript_compaction.py` | 去重 pass；pressure 窗口分支（+ `CompactionWindow.tail_tokens`，F3）；一行替换 + 净收益门；新标记正则；**`select_summary_region`（连续前缀段、吸收旧摘要，F2）** | §2.1-2.3、§3.3 |
| `core/context/transcript_summary.py`（**新**） | 输入组装、模板常量、trim-retry、摘要消息构造 | §3.3-3.4、3.7 |
| `core/rollout.py` | `tool_success` 映射（投影类 + 投影器） | §2.4 |
| `serve/agent/db/rollout_compaction.py` | `maybe_compact_async`；策略选择；dry-run 分支；失败冷却 | §3.1-3.2、3.6、3.9 |
| `core/agent/util/llm/llm_client.py` | 支持"显式 None = 不设上限"（或适配层过滤） | §3.5、F7 |
| `core/context/budget.py` + `app/config.py` | §4 全部新字段（默认值一致） | §4 |
| `app/openapi/api_v1/agentic_data_api.py` | loader 包装改 `await maybe_compact_async`；传入 `llm_client` / `model_name` | §3.1 |
| 测试 | 见 §6 | — |

---

## 6. 测试与验收

### 6.1 去重（~7 例）
重复内容保最新、旧副本变引用行；多重复；保护区内副本作为"保留那份"；只替换可压区；幂等（第二次无操作）；`<200` 字符不参与；**F1：保留副本可剪 → 不去重（两份都被剪成一行）**；**persisted 块不参与（F8）**。

### 6.2 泄压阀（~5 例）
死锁形态（正常窗口空 + 保护区超软预算）→ 触发；阶段 A/B/C 顺序与硬底线（活跃用户消息不动）；persisted 块不被降级；未闭合调用 turn 不降级（已知局限用例）；reclaim 仍过回收门；**`tail_tokens` 计算正确（F3）**。

### 6.3 一行形态（~6 例）
格式与三字段来源正确；`success` 缺失 → `?`；args 换行被替换；净收益门（极端长 args 下不剪）；三个幂等标记各自跳过；persisted 块仍跳过。

### 6.4 摘要生产者（~9 例）
策略选择（达阈值走 summary / 未达走 prune）；输入无截断（断言长内容完整出现）；trim-retry（mock 超窗 → 裁剪重试成功；耗尽 → no-op）；空摘要 → no-op 且回退 prune；模板开关三值；无 wire cap（mock 捕获，按后端形态断言）；append 失败 → 无变化；重启后重建一致（含摘要）；**F2：可压区被旧摘要段/未闭合 turn 穿插时，区域取连续前缀段且旧摘要被吸收**。

### 6.5 三态与回归
`flag=off` 全链路一致；`dry-run` 零事件零 LLM（mock 断言未调用）；`flag=on` 正常写入 + 投影中段摘要 + 请求包含摘要视图。

---

## 7. 提交分组（6 组，保持可审）

```
1. P1-① 去重
2. P1-② 泄压阀
3. P1-③ 剪枝形态（一行 + success 透传 + 净收益）
4. P2-⑦ 摘要生产者 + 事件化提交 + token 重算 + lease + 旧摘要过滤
5. P2-⑧ 无硬帽 + trim-retry + 结束标记
6. P2-⑫ 模板三版本 + 开关
```

> ⚠️ **组 4/5 必须同批合入（F5）**：组 4 单独上线 = 带 wire cap、无 trim-retry 兜底——**不得单独启用**。

---

## 8. 风险与已知局限

| # | 风险/局限 | 缓解 |
|---|---|---|
| 1 | 异步结构改动主链路 loader 包装 | flag 默认关 + 三态回归兜底；同步内核保留 |
| 2 | 超窗分类靠文本匹配（无现成异常类型） | best-effort + 日志留错误原文，后续补模式 |
| 3 | dry-run 不覆盖摘要质量 | 独立评估活动（模板 A/B） |
| 4 | pressure 不覆盖"未闭合调用 turn 内"的死锁 | 已知局限，记录；观测到再评估 |
| 5 | 详情找回 = 重跑：**不可重现结果会永久丢失** | 已接受（Hermes 同）；`<persisted-output>` 路径仍保留 |
| 6 | legacy `compact.py` 不动 | 非 rollout 路径维持现状（§9-D5） |
| 7 | **摘要调用在请求关键路径**（达阈值时每步一次 LLM 调用，延迟依赖 provider，客户端无超时参数） | 失败/超时→回退剪枝；观测"摘要调用时长"；cooldown 防重试风暴 |
| 8 | **去重引用行的残留场景**（保留副本日后老化被剪） | 影响轻微（详情=重跑）；记录不修 |

---

## 9. 决策记录（2026-10-14）

| # | 事项 | 决策 |
|---|---|---|
| D1 | Phase 1 范围 | 三项（去重/泄压阀/剪枝形态）；与 Phase 2 **合并为一批** |
| D2 | 剪枝形态 | **激进一行**（args 预览 + success + 大小）；**不落盘、不建模板**；详情找回 = 重跑同一调用 |
| D3 | `success` 来源 | 投影新增 `tool_success` 映射；**不改 replacement schema** |
| D4 | 模板默认 | `hybrid`（可切换；A/B 评估另行） |
| D5 | legacy `compact.py` | **不动**（含 `content[:3000]` 与 `max_new_tokens=2000`）——早前"移除 `[:3000]`"的范围收窄为"**新生产者不预裁剪**"；理由：legacy 无 trim-retry 兜底，改它有超窗风险 |
| D6 | dry-run 摘要 | 不调 LLM、不写事件；只记"将会摘要"的估算 |
| D7 | 失败冷却 | 内存计数（3 次）+ 60s 冷却；非持久化（接受） |
| D8（v2） | 去重生效条件 | 保留副本**不会被剪**时才生成引用行（F1）；persisted 不参与去重（F8） |
| D9（v2） | 摘要区域 | **`select_summary_region`**：连续可压前缀段 + 吸收旧摘要（F2） |
| D10（v2） | 无帽实现 | **显式 `None` 透传**（不设上限）；按部署后端断言（F7） |

> ⚠️ **D5 是唯一"收窄早前决策"的项**——如需 legacy 也同步移除 `[:3000]`，请在评审时指出（代价见上）。
