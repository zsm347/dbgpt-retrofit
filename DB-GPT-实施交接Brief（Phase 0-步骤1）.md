# DB-GPT Phase 0 实施交接 Brief —— 步骤 1：投影 / 事件层

> ⚠️ **已被取代（2026-10-13）**：本文件是"步骤 1 单步版"。执行请改用《[DB-GPT-实施交接Brief（Phase 0-全量）](./DB-GPT-实施交接Brief（Phase 0-全量）.md)》（步骤 1+2+3 一次交付）；本文件仅作历史留存。
>
> **用途**：本文件是**自包含**的施工任务书——把它 + 下面两份配套材料交给执行 agent，即可开工，不需要任何额外对话上下文。
>
> **配套材料（都在仓库里，阅读顺序）**：
> 1. 《[DB-GPT-上下文压缩改造设计（Phase 0）](./DB-GPT-上下文压缩改造设计（Phase 0）.md)》——**完整设计**，凡本 Brief 与它冲突，以它为准；
> 2. 《[DB-GPT-主链路评估与改进清单](./DB-GPT-主链路评估与改进清单.md)》——D.15（现状复核）/ D.16（Phase 0 定义）/ P1-25。
>
> 标注约定（沿用上述文档）：**已核实** = 源码一手核对（带 `file:line`）；**推断** = 合理推理未验证；**建议** = 设计选择。执行方报告时同样遵守此约定。

---

## 0. 30 秒背景 + 任务一句话

**背景（已核实，详见 D.15）**：DB-GPT 主链路已切换到 rollout 事件投影（原生 `role=tool` 消息、每步 `load_projection` 重建请求），但压缩体系仍按旧文本 ReAct 结构设计——L1/L2/L3 全部不触发、压缩结果未接入事件流。Phase 0 分步修复"机制层"；**本次只做第 1 步**。

**任务一句话**：在事件层落地 `context_compacted` 事件的 **schema + 投影应用 + 投影类扩展 + 单测**（设计文档 §4 / §6.2 / §8 前两行 / §10.1 第 1 步）。**不加生产者、不改行为、不开 flag。**

---

## 1. 仓库与基线（⚠️ 开工前必做）

| 项 | 值 |
|---|---|
| 仓库 | `/Users/zsm/Documents/DB-GPT` |
| **基线分支** | `wip/rollout-ltm-pilot-20261013`（**工作区已全部提交、clean——开工时 `git status --short` 应为 0 项**） |
| 提交栈 | `ca9f014c`（上游基点）→ `6aaa69c7` rollout 基础设施 → `7d6ad7c1` 模型层 → `2285b596` 长期记忆 → `e1221319` 设计文档 + pilot |
| 远程 | `origin` = `https://github.com/zsm347/dbgpt-retrofit`（**私有，你自己的，可推送**）；`upstream` = eosphoros-ai/DB-GPT（**只读参考——禁止 push**） |
| 网络提示 | 本机 git 访问 GitHub 需走代理（gh 命令不受影响）：`git -c http.proxy=http://127.0.0.1:7897 <命令>` |

**纪律**：
- ✅ 开工前：`git status --short`（应为 clean）+ `git log --oneline -5`（应与上表提交栈一致）
- ✅ 你的改动正常提交到 `wip/rollout-ltm-pilot-20261013`；**仓库未配置 git 身份** → 提交请用一次性参数：`git -c user.name=zsm347 -c user.email=zsm347@users.noreply.github.com commit -m "..."`（或先自行 `git config --local` 配置）
- ❌ 禁止 push 到 `upstream`（只读参考）；禁止 `reset --hard` / `rebase` 既有 4 个提交；禁止删除 `.gitignore` 中新增的排除规则（`data/longmemeval/`、`.tmp_mrag_check/`、`pilot/experiments/`、`pilot/parsed/`、`pilot/.venv-rag-eval/`）
- ❌ 不修改**其他工作流**已提交的文件（长期记忆、pilot、模型层）——除非任务明确要求

---

## 2. 事实来源（只信设计文档，不要重新发明）

| 需要什么 | 看哪里 |
|---|---|
| 事件 schema 与校验规则 | 设计文档 **§4.1 / §4.2** |
| 投影应用伪代码 | 设计文档 **§4.3** |
| 投影类扩展（`latest_event_seq` / `last_compaction`） | 设计文档 **§6.2（F1）** |
| 单测清单 | 设计文档 **§9.1** |
| 已拍板的决策（**不得改动**） | 设计文档 **§11** |

⚠️ 遇到"设计文档与代码不符" → **停下来报告**，不要自行改设计、不要"顺手修"。

---

## 3. 任务包（只做这些，三个文件）

### A. `packages/dbgpt-core/src/dbgpt/agent/core/rollout.py`

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

### B. `packages/dbgpt-serve/src/dbgpt_serve/agent/db/agent_event_db.py`

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

### C. 测试

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

## 4. 明确不做（本期红线）

- ❌ 协调器 / `maybe_compact` / loader 挂载（设计文档 §2.3、§10.1 第 3 步）
- ❌ P1-19 ①/②（第 2 步；且 **P1-19 ② 已定不接线**）、P1-23 read_file 帽（第 2 步）
- ❌ `compact.py` 的 L1/L2/L3 重写（后续任务）
- ❌ UI / 前端 / SSE 状态改动
- ❌ 不开任何 flag、不写任何生产者——**本步骤结束时系统行为必须与现在完全一致**

---

## 5. 环境与命令

```bash
cd /Users/zsm/Documents/DB-GPT
uv sync                                        # 环境未就绪时

# 目标测试
uv run pytest packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py -v
uv run pytest packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_agent_event_db.py -v

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

## 6. 验收标准

1. 上述新增单测（1–7）全部通过
2. 既有测试**无新增失败**；若基线存在既有失败，**先记录并报告**，不要顺手修无关问题
3. 新事件可写入（DAO 校验通过）且可投影（无 / 一次 / 多次三态正确）
4. **行为零变化**：现有会话的投影输出与改动前一致（由测试 1 证明）

---

## 7. 交付物格式（报告模板）

```
① 改动清单：文件 → 行区间 → 对应设计文档章节
② 测试：命令 + 结果（原始输出摘要，红/绿明确）
③ 与设计文档的偏差：有 / 无（有则逐条说明）
④ 未决 / 风险
⑤ git status --short 前后对比（只应多出你的改动）
```

---

## 8. 纪律

- 结论分"**已核实 / 推断 / 建议**"三类；禁止无证据的"完成"汇报，每条验收必须贴命令与输出
- 改动与设计文档章节一一对应；不夹带无关重构（格式化、改名、依赖升级一律不做）
- 设计与代码不符、或触发任何红线 → **停止并报告**，等待指示
