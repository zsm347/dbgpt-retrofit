# DB-GPT 实施交接 Brief —— Phase 1+2（去重 / 泄压阀 / 剪枝形态 / 摘要生产者）

> **用途**：本文件是**自包含**的施工任务书——交给执行 agent，可一次完成 Phase 1+2 全部代码（**6 组提交**），不需要任何额外对话上下文。
>
> **配套材料（都在仓库里，阅读顺序）**：
> 1. 《[DB-GPT-上下文压缩改造设计（Phase 1+2）](./DB-GPT-上下文压缩改造设计（Phase 1+2）.md)》——**权威设计（v2，2026-10-14）**，凡本 Brief 与它冲突，以它为准；
> 2. 《[DB-GPT-上下文压缩改造设计（Phase 0）](./DB-GPT-上下文压缩改造设计（Phase 0）.md)》——前置机制（**已完成、已验收**）；
> 3. 《[DB-GPT-主链路评估与改进清单](./DB-GPT-主链路评估与改进清单.md)》——D.15/D.16/P1-25 背景。
>
> **本任务不包含**：上线灰度（dry-run 观察 → 小流量 → 默认）——那是代码合入后的**部署程序**，由部署方执行。
>
> 标注约定：**已核实** = 源码一手核对（带 `file:line`）；**推断**；**建议**。执行方报告同此。

---

## 0. 背景与任务一句话

Phase 0 已把机制层建好并通过验收（`context_compacted` 事件 / 投影整体替换 / 协调器 / 剪枝生产者 / 触发链；flag 默认关）。本批 = 在其上做六件事：

```
P1：① 去重  ② 泄压阀  ③ 剪枝形态（激进一行）
P2：⑦ 摘要生产者  ⑧ 无硬帽+trim-retry+结束标记  ⑫ 模板三版本+开关
```

**依赖关系**：任务 1/2/3 相互独立；任务 4 依赖 3（复用一行/信号与投影映射）；任务 5/6 依赖 4。**组 4/5 必须同批合入（不得只合 4 就启用）**。

---

## 1. 仓库与基线（⚠️ 开工前必做）

| 项 | 值 |
|---|---|
| 仓库 | `/Users/zsm/Documents/DB-GPT` |
| **基线分支** | `wip/rollout-ltm-pilot-20261013`（除 `pilot/` 外工作区应为 clean） |
| 基线提交 | 以开工时 `git log --oneline -3` 为准（最新应含 `docs(design): revise Phase 1+2 design to v2`） |
| 远程 | `origin` = `https://github.com/zsm347/dbgpt-retrofit`（**私有，可推送**）；`upstream` = 官方只读（**禁 push**） |
| 网络提示 | git 访问 GitHub 需走代理（gh 命令不受影响）：`git -c http.proxy=http://127.0.0.1:7897 <命令>` |
| 身份提示 | 提交用一次性参数：`git -c user.name=zsm347 -c user.email=zsm347@users.noreply.github.com commit -m "..."` |

**纪律**：
- ✅ 开工/收工各做一次 `git status --short`（收工时只应多出你的改动）
- ⚠️ **若 `pilot/` 下有未提交改动**：那是**另一个活跃工作流**的进行中改动——**不要动、不要提交、不要回滚**；收工对比把它排除在外
- ✅ 提交到本分支、可推送（代理参数）；提交信息英文 conventional（`feat(agent): ...` / `fix(agent): ...`）
- ❌ 禁止 push 到 `upstream`；禁止 `reset --hard` / `rebase` 既有提交；禁止删除 `.gitignore` 规则
- ❌ **不开任何 flag**（`context_compaction_enabled` / `context_compaction_dry_run` 保持默认 `false`）

---

## 2. 事实来源（只信设计文档，不要重新发明）

| 需要什么 | 看哪里 |
|---|---|
| 去重条件与边界（含 F1 生效条件） | 设计文档 **§2.1** |
| 泄压阀触发/三阶段/硬底线（含 F3 `tail_tokens`） | §2.2 |
| 一行格式/字段来源/净收益/幂等标记 | §2.3 |
| `tool_success` 映射（含 F8 不清理） | §2.4 |
| 异步结构与延迟（F6） | §3.1 |
| 触发与策略选择 | §3.2 |
| 摘要区域/序列化/trim-retry（F2/F4） | §3.3 |
| 摘要消息形态与旧摘要（F2 同步） | §3.4 |
| 无帽按后端处理（F7） | §3.5 |
| 失败冷却 | §3.6 |
| 模板三版本 | §3.7 |
| 事件提交与记账 | §3.8 |
| dry-run 语义 | §3.9 |
| 配置表 / 改动清单 / 测试清单 | §4 / §5 / §6 |
| **决策记录（不得改动）** | **§9** |

⚠️ 遇到"设计文档与代码不符" → **停下来报告**，不要自行改设计、不要"顺手修"。

---

## 3. 任务包 1：P1-① 去重（提交组 1）

**文件**：`packages/dbgpt-core/src/dbgpt/agent/core/context/transcript_compaction.py`

- 新增纯函数（建议 `dedupe_replacement(...)`，或等价方案）：**去重先行、剪枝后行**（协调器在构建 replacement 时先应用去重结果）
- 规则（照 §2.1）：
  - 候选：tool 消息，`len(content) ≥ config.dedup_min_chars`（默认 200），不含三个已处理标记（§2.3）；**`<persisted-output>` 块不参与（F8）**
  - 指纹：`hashlib.md5(content.encode("utf-8"))`；从**最新**向最老扫描，同指纹**保留最新一份全文**
  - **F1 生效条件**：仅当【保留副本不会被剪】——`在保护区 / len ≤ min_prune_chars / 是 persisted 块`——才把更老副本（落在 `window.compressible_indices` 内）替换为引用行：
    `[duplicate of the later {tool_name} result — content omitted]`
    否则**不生成引用行**（交给剪枝把两份都剪成一行）
  - `tool_name` 来源：同 turn ai 消息 `tool_calls` 的 `tool_call_id → name` 映射；缺失用 `"tool"`
- **统计**：去重与剪枝**共用一次 accounting**（`tokens_before` = 原始；`tokens_after` = 最终）
- 配置：`dedup_min_chars`（`budget.py` 的 `ContextBudgetConfig` + `dbgpt_app/config.py` 的 `AgentContextParameters`，默认值一致）
- 测试：设计文档 §6.1（7 例），扩展 `context/tests/test_transcript_compaction.py`

---

## 4. 任务包 2：P1-② 泄压阀（提交组 2）

**文件**：`transcript_compaction.py`

- `CompactionWindow` **新增字段 `tail_tokens: int`**（F3）：`select_window` 计算尾部保护区各 turn 的估计 token 和并返回
- `select_window(..., pressure: bool = False)`：pressure 分支按 §2.2 三阶段
  - A：保护区内、**当前 turn 之外**的大工具结果 → 一行（从最老开始，够用即停）
  - B：仍超 → 除**最新一条工具结果**外，保护区内所有工具结果降级
  - C：仍超 → **最新一条工具结果也降级**（绝对最后手段）
- **硬底线**：活跃用户消息（当前 turn 的人类消息）**永不降级**；`<persisted-output>` 块跳过；未闭合调用 turn 保护（**已知局限写注释**：此类 turn 内的死锁泄压不生效）
- `decide_compaction`：正常窗口 → 若（`window.empty` 或 `stats.reclaimed < min_reclaim_tokens`）**且** `tail_tokens > int(protect_tail_tokens × pressure_soft_factor)` → pressure 窗口 → 仍不行才拒绝；`gate="pressure"` 进日志/审计
- 配置：`pressure_soft_factor`（默认 1.5）
- 测试：设计文档 §6.2（5 例，含 `tail_tokens` 计算正确性）

---

## 5. 任务包 3：P1-③ 剪枝形态（提交组 3）

**文件**：`transcript_compaction.py` + `packages/dbgpt-core/src/dbgpt/agent/core/rollout.py`

- `build_pruned_replacement` 的替换文本改为**激进一行**：
  ```
  [{tool_name}] args≈{arguments 预览，换行→空格，≤ prune_args_preview_chars} · success={true|false|?} · {N} chars
  ```
  - `name/args`：同 turn ai 消息 `tool_calls` 映射（缺失 → `[tool] args≈?`）
  - `success`：从投影新增映射（下条）；缺失 → `?`
  - **净收益门**：`len(original) − len(new_line) ≥ config.min_saving_chars`（默认 500）才替换
- `rollout.py`：
  - `ChatCompletionHistoryProjection` 新增 `tool_success: Mapping[str, bool] = field(default_factory=dict)`
  - 投影器 `tool_result` 分支：读 `payload.get("success")`，`isinstance(..., bool)` 才记录
  - **replacement 分支不清理该映射**（F8：call id 不变、条目仍有效；残留无害）
- **幂等**：新增正则识别新格式；**保留**旧格式（`… [truncated, total N chars]`）与引用行（`^\[duplicate of the later `）的识别（§2.3 三条）
- 配置：`min_saving_chars`（500）、`prune_args_preview_chars`（80）
- 测试：设计文档 §6.3（6 例）

---

## 6. 任务包 4：P2-⑦ 摘要生产者（提交组 4，依赖任务 3）

**文件**：`transcript_compaction.py` + **新文件** `context/transcript_summary.py` + `rollout_compaction.py` + `agentic_data_api.py`

- `transcript_compaction.py`：新增 **`select_summary_region`**（F2）——取【从最老消息起、**连续**的可压前缀段】，遇**结构保护**（未闭合调用 turn / 无主段 / 当前 turn / 尾部保护区）即停止；**旧摘要段不阻断**（进入输入、被吸收）；顺带给出 `region_tokens`
- 新文件 `transcript_summary.py`：
  - 输入组装（§3.3）：`[{role}]: {content}`（**全量，不截断**）+ ai 带 tool_calls 时 `[tool_calls] {name}({arguments})`
  - 模板常量：**本组先迁移六段模板**（自 `compact.py:10-37` 文本）
  - 摘要消息构造：`[Context Summary of {N} earlier messages]\n\n{summary}\n\n`（**结束标记在组 5 追加**）
  - 过渡形态：本组可先带 `max_new_tokens`、无 trim-retry；组 5 去帽 + 加重试（**两组必须同批合入**）
- `rollout_compaction.py`：`maybe_compact_async`（§3.1）
  - async 入口；策略选择 **summary 优先**（`est ≥ compact_threshold_tokens_for(...)`，再守 rearm 门与 §3.2 门 3 的 assumed 回收）；否则 prune；否则 no-op
  - **摘要任何失败 → 回退尝试 prune → 仍不行 no-op**；never-raise 契约
  - dry-run：**不调 LLM、不写事件**，记 §3.9 字段（F6：注释说明"摘要调用在请求关键路径、时间依赖 provider"）
  - append payload：`reason="threshold"`、`strategy="summary"`、`accounting` 含 `summary_chars`；`rearm` 用真值
- `agentic_data_api.py`：`_load_rollout_projection` 包装改 `await maybe_compact_async(...)`，传入闭包内的 `llm_client` 与 `model_name`
- 测试：设计文档 §6.4 的前 8 例（trim-retry / 无帽两例归组 5）

---

## 7. 任务包 5：P2-⑧ 无硬帽 + trim-retry + 结束标记（提交组 5）

**文件**：`llm_client.py` + `transcript_summary.py`

- `packages/dbgpt-core/src/dbgpt/agent/util/llm/llm_client.py`：支持**显式 `None` = 不设上限**并透传（F7，已核实：本地后端 `params.get("max_new_tokens", 2048/4096)` 会落默认帽；远程 provider 路径不传通常无上限）——按实际部署形态处理并在单测断言
- `transcript_summary.py`：
  - 调用改**直调** `llm_client.create(...)`（**不复用 `generate_text`**——它吞异常返回空串，无法支撑 trim-retry）
  - `_is_context_overflow_error(exc)`：8 个文本模式（§3.3）；**trim-retry**：从最老 turn 裁掉一个 → 重试；**终止 = 只剩 1 个 turn**；`summary_trim_max_attempts`（8）**只约束"无进展的重试"**（F4）
  - 摘要消息**追加结束标记**：`--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---`（Hermes 原文）
- 测试：设计文档 §6.4 的 trim-retry 两例（成功 / 有界放弃）+ 无帽断言（按部署形态）

---

## 8. 任务包 6：P2-⑫ 模板三版本 + 开关（提交组 6）

**文件**：`transcript_summary.py` + 配置文件

- 补 **handoff**（Codex 四段）与 **hybrid**（合体第三版：六段骨架 + 用户约束/偏好槽 + handoff framing + "已完成勿重做"声明）模板（§3.7）
- `summary_template` 选择逻辑（默认 `"hybrid"`）；配置进 `budget.py` + `app/config.py`
- 测试：模板三值切换断言（单测捕获所用 prompt）

---

## 9. 明确不做（本批红线）

- ❌ **不开 flag**（`context_compaction_enabled` / `dry_run` 保持 false）
- ❌ **不动 `compact.py`**——含 `content[:3000]` 与 `max_new_tokens=2000`（§9-D5：legacy 无 trim-retry 兜底）
- ❌ **不做剪枝时落盘**（详情找回 = 重跑同一调用）；**不建 per-tool 模板**
- ❌ 不做 Phase 3（微压缩 / 主动剪枝低水位 / 服务端压缩）；**不做模板 A/B 评估活动**
- ❌ 不动 UI / 前端；不动其他工作流（pilot / 长期记忆 / 模型层）
- ❌ 不做无关重构（格式化、改名、依赖升级一律不做）

---

## 10. 环境与命令

```bash
cd /Users/zsm/Documents/DB-GPT

# 目标测试（用仓库自带 .venv；uv run 亦可）
.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests/test_transcript_compaction.py \
  packages/dbgpt-core/src/dbgpt/agent/core/tests/test_rollout.py \
  packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests/test_rollout_compaction.py -q

# 本批新增测试文件（摘要生产者的单测）
.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests/test_transcript_summary.py -q

# 收工前的回归范围（推荐）
.venv/bin/python -m pytest \
  packages/dbgpt-core/src/dbgpt/agent/core/tests \
  packages/dbgpt-core/src/dbgpt/agent/core/context/tests \
  packages/dbgpt-serve/src/dbgpt_serve/agent/db/tests \
  packages/dbgpt-app/src/dbgpt_app/tests -q
```

（注意：仓库根的 `make test` = `pytest --pyargs dbgpt`，**不覆盖 serve/app**——用上面的显式路径。）

---

## 11. 验收标准（照设计文档 §0.3，8 条）

1. §6 全部单测通过；既有测试无新增失败
2. `flag=off`：与改动前逐字节一致（回归）
3. `dry-run`：P1 变更照常试算；**P2 只记"将会摘要"、不调 LLM、不写事件**
4. `flag=on`：剪枝为一行格式（含 `success`）；摘要路径：事件写入、投影中段摘要、**进程重启后重建一致**
5. trim-retry：mock 超窗 → 逐条裁剪重试 → 成功 / 有界放弃（各一例）
6. 模板开关三值可切换
7. **无 wire cap**：按实际部署形态断言（远程：无上限字段；本地后端：显式覆盖默认帽）
8. 兼容幂等：Phase 0 旧截断痕迹与去重引用行不被二次处理

---

## 12. 交付物格式（报告模板）

```
① 改动清单：文件 → 行区间 → 对应设计文档章节
② 测试：命令 + 结果（原始输出摘要，红/绿明确）
③ 与设计文档的偏差：有 / 无（有则逐条说明）
④ 未决 / 风险
⑤ git status --short 前后对比 + git log 新增提交列表（6 组）
```

---

## 13. 纪律

- 结论分"**已核实 / 推断 / 建议**"三类；禁止无证据的"完成"汇报，每条验收必须贴命令与输出
- 改动与设计文档章节一一对应；不夹带无关改动
- 设计与代码不符、或触发任何红线 → **停止并报告**，等待指示
