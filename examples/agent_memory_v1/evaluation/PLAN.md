# LongMemEval-S 接线计划

## 1. 目标

- run id: `longmemeval-s-adapter-v0`
- 当前工作：把官方 LongMemEval-S 的时间顺序 history 回放到现有 `MemoryService`，再用同一 service 检索候选并调用回答模型，输出官方 QA scorer 可读的 JSONL。
- 研究问题：当前记忆链路能否在真实多会话输入上保持信息形成、更新后使用、时间问题和无法回答问题的基本正确性？
- 本轮不修改记忆写入/召回语义；只做数据适配、运行记录和评分接线。

## 2. 对照与口径

- 数据集：LongMemEval 官方 `longmemeval_s_cleaned.json`，先取可重复的小样本，再扩大到完整 S。
- 单题隔离：每题使用独立 scope 和空的 Qdrant 数据目录；按 `haystack_sessions` 的顺序回放，不把问题、金标准或 evidence 标签写入记忆。
- 主要输出：`question_id`、类型、答案、召回候选（含来源 ID）、耗时、错误状态。
- 官方 QA 指标：Overall Accuracy、六类 question type accuracy、Task-averaged Accuracy、Abstention Accuracy。
- 额外可选：从 source refs 映射的候选来源做 Recall@K/nDCG；必须注明这是来源重合，不等于合并正文完整保留 evidence。
- 不能用本轮小样本宣称整体质量通过；小样本只先验证接线、隔离、格式和失败可追溯性。

## 3. 代码计划

| 路径 | 作用 | 变化 |
| --- | --- | --- |
| `evaluation/longmemeval.py` | 新增官方 JSON 解析、chronological replay、检索/回答记录 | 只依赖现有 `MemoryService` 与 `build_answer_prompt` |
| `evaluation/README.md` | 说明数据下载、环境变量、命令和输出 | 不复制官方 scorer |
| `evaluation/PLAN.md` / `CHECKLIST.md` | 记录本轮契约与执行状态 | 持续更新 |
| `docs/agent_long_term_memory_prototype_eval.md` | 记录正式评测已开始及当前限制 | 只在接线完成后更新 |

## 4. 执行设计

- 最小 smoke：本地内置 synthetic case，使用 fake service/answerer，验证消息顺序、scope 隔离、问题不写入和 JSONL 格式。
- API pilot：先运行 5 题接线试跑，再用固定抽样补齐其余 question type；当前记录的 DeepSeek 运行使用 `deepseek-v4.1-flash`（thinking 开启）和 `text-embedding-v3/1024`，每题独立目录。每次运行记录实际模型与配置，不把不同模型结果混为一组。
- 正式运行：接线确认后由调用者显式选择 20–30 题或完整 S；不在本轮自动消耗完整 API 配额。
- 停止条件：smoke 失败、输入字段不兼容、某题写入/检索跨 scope，先修接线；不因模型答案单题错误直接修改记忆规则。
- 输出目录：`evaluation/runs/<run_id>/`，不把数据集和可能含敏感文本的运行结果提交到仓库。

## 5. 风险与降级

- 无 DashScope key：仍可运行 parser/replay smoke；API pilot 明确标记为未执行。
- 官方数据下载失败：允许传入本地 JSON 路径；不自造 benchmark 数据替代官方数据。
- 回答模型不可用：保留 retrieval-only 记录，不能生成 QA accuracy。
- 官方 GPT-4o scorer 未接入时，不把 Qwen 自评当作官方分数；先保存预测 JSONL，评分协议另行记录。

## 6. 版本记录

| 时间 | 变化 | 原因 |
| --- | --- | --- |
| 2026-10-12 | 建立 LongMemEval-S 薄适配计划 | 现有系统已完成工程链路，下一步转正式效果评测 |
| 2026-10-12 | 完成 5 题初始 API pilot，并完成额外 5 题类型扩展 | 初始 5 题均为 `single-session-user`；后 5 题各覆盖一种未测类型，先检查多会话、偏好、时间、知识更新和 assistant 内容 |
| 2026-10-12 | 增加 1 道带 `_abs` 后缀的弃答诊断题 | 前 10 题覆盖六种常规类型，但没有覆盖 LongMemEval 单独标记的弃答题 |
| 2026-10-12 | 核对官方 QA scorer 的运行前提 | 上游 scorer 的任务标准可复用；汇总脚本固定 GPT-4o-2024-08-06。DashScope 替代评委需标为协议变体 |
| 2026-10-12 | 使用 DashScope `qwen3.8-max` 完成 11 题诊断评分 | 9/11；与人工对照的两道失败一致。结果为自定义 judge 小样本分数，不是官方/论文可比结果 |
| 2026-10-13 | 完成固定 seed 的 14 题分层 pilot，并用 DashScope `qwen3.8-max` 评分 | 13/14；含六类题型与 2 道弃答题。运行期间出现的锁冲突和超时通过隔离重跑恢复，最终评分只使用每题一条有效预测 |

## 当前状态（2026-10-13）

适配器、解析单元测试、无模型 dry-run 和 1 题 mini fixture API smoke 已完成。官方 cleaned 数据已下载。初始 5 题和类型扩展 5 题已运行；扩展覆盖 `multi-session`、`single-session-preference`、`temporal-reasoning`、`knowledge-update`、`single-session-assistant`，共回放 240 段历史，DeepSeek V4.1 Flash（thinking 开启），整题运行错误为 0，耗时约 85 分钟。随后增加 1 道 `_abs` 弃答诊断题，回放 53 段历史，耗时约 20 分 21 秒、运行错误为 0；记忆检索在 Top-10 中找到了“番茄 5 株”记录（第 3 位），没有辣椒数量，回答正确给出已知数量并拒绝推断总数。

截至目前 11 道人工对照样本中，9 道符合参考答案；这只是经过挑选的小样本诊断，不是整体准确率。两道失败分别是：优惠券问题回答时没有关联“Target”与优惠券记忆；歌曲问题的原会话来源被命中，但具体音符没有保留在记忆正文。后一例说明来源会话 Recall@K 不能证明答案所需细节已保存。弃答样本只有 1 道，不能据此判断整体弃答能力。

随后使用 DashScope `qwen3.8-max` 对同一批 11 道题运行了自定义 judge，结果为 **9/11（81.82%）**，六类已出现题型的平均准确率为 **80.00%**；按题型为 `single-session-user` 4/5、`single-session-preference` 1/1、`single-session-assistant` 0/1、`multi-session` 2/2、`temporal-reasoning` 1/1、`knowledge-update` 1/1。唯一弃答样本 1/1。两道 NO 与人工复核的两道失败相同。以上数字仅描述这 11 道挑选的诊断题，类别分母很小，尤其弃答只有一道；不能视为完整 LongMemEval-S 准确率或官方/论文可比成绩。

### 2026-10-13 固定分层 14 题 pilot

- 抽样：使用 seed `20261012`，六种常规 `question_type` 各 2 题，另加 2 道官方 `_abs` 弃答题；合计 14 题、670 个历史 session。选择清单排除了此前已运行的 question ID。固定列表和问题类型见 `data/longmemeval/selections/20261012_stratified14_seed20261012.json`。
- 配置：DeepSeek V4.1 Flash（thinking 开启）负责记忆整理和作答；DashScope `text-embedding-v3`，1024 维；Mem0 2.2.1、LangMem 0.0.30、Qdrant 1.19.1。评分为 DashScope `qwen3.8-max` 自定义 judge，temperature 0，不是 LongMemEval 官方 GPT-4o 评分。
- 结果：Overall **13/14（92.86%）**；六种已出现题型的 macro/task average **91.67%**；弃答 **2/2（100%）**。按题型：`single-session-user` 2/2、`single-session-preference` 2/2、`single-session-assistant` 1/2、`multi-session` 3/3、`temporal-reasoning` 3/3、`knowledge-update` 2/2。`multi-session` 和 `temporal-reasoning` 的分母各包含 1 道弃答题。
- 唯一错误：`7161e7e2` 问 Admon 周日的具体班次。最终回答选择不猜，但只从记忆中找到“生成了一周轮班表”，缺少该员工具体的班次。gold 来源 session 出现在 Top-10 候选里，因此当前证据更支持“提取/整理时遗漏答案细节”，不是来源 session 没召回。
- 执行记录：初次并发运行触发 Mem0 本地 Qdrant migration 目录锁；之后的多题长进程在时间推理题遇到模型超时，并使同进程后续题无法打开本地 Qdrant。对失败题改为单题独立进程重跑；每道最终采用的预测均非空，qwen judge 输入有 14 个唯一 ID。上述执行错误没有当作系统答错计分。
- 检索配置限制：运行提示 `fastembed not installed`，因此 Mem0 的 BM25 关键词搜索通道未启用。本次结果是当前向量语义检索配置的表现。
- 解释边界：该分层小样本可以比较本轮选中的能力类型并暴露具体失败，但每类仅 2 个基础样本，不能据此认定整体系统准确率达到 92.86%，也不能与官方/论文 GPT-4o 成绩横向比较。
- 评分和预测产物：`data/longmemeval/runs/20261013_deepseek_v41_flash_stratified14_qwen38_judge/`；逐题答案、检索候选和写入审计保存在对应的 2026-10-12/13 `stratified14` run directories。

评分适配器复用 LongMemEval 的题型判分思路，但通过 DashScope API 调用 `qwen3.8-max`，提示词为本项目的适配版本。上游 `print_qa_metrics.py` 固定 GPT-4o-2024-08-06，因此当前结果应称“DashScope 自定义 judge 分数”，不能称作官方 GPT-4o 分数。[官方 QA evaluator](https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py) 和[官方指标汇总脚本](https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/print_qa_metrics.py)。运行记录见 `data/longmemeval/runs/20261012_qwen3_8_max_judge11/`，预测来自此前 5 题 pilot、5 题类型扩展和 1 道弃答样本的去重合并。

### 弃答诊断题记录

- 题目：`6456829e_abs`，类型 `multi-session`；问题询问最初种了多少番茄和辣椒。
- 参考答案：资料只说明种了 5 株番茄，没有说明辣椒数量。
- 系统回答：说明记录中有 5 株番茄，但没有辣椒数量，因此不能给出两种作物的总数。人工核对符合参考答案。
- 证据检查：Top-10 第 3 条记忆记录了番茄数量为 5；没有检索结果声称辣椒数量。回答没有把已知的番茄数量扩展成总数。
- 执行：53 段历史，DeepSeek V4.1 Flash（thinking 开启），`text-embedding-v3/1024`，约 20 分 21 秒，`run_errors=0`。记忆管理器汇总 74 次 add、2 次 update、490 次 skip，无 failed/unknown/unattempted action。
- 运行目录：`data/longmemeval/runs/20261012_deepseek_v41_flash_abstention1/`。单题通过只证明该样本处理正确，不代表弃答子集的总体表现。
