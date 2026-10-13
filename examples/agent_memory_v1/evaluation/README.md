# LongMemEval-S 评测接线

`longmemeval.py` 是当前记忆原型的薄适配器。它不改变 `MemoryService` 的提取、写回或召回规则，只做四件事：

1. 读取官方 LongMemEval cleaned JSON；
2. 每道题建立独立 scope 和本地 Qdrant 目录，按 haystack session 的顺序写入历史；
3. 用问题调用现有 `search()`，把候选、来源、时间和回答保存下来；
4. 输出官方 scorer 使用的 `predictions.jsonl`：每行 `{"question_id": ..., "hypothesis": ...}`。

## 先做无模型接线检查

准备一个官方数据文件后，可以只检查 JSON、字段、会话顺序和输出目录，不需要 API key：

```bash
.venv/bin/python -m evaluation.longmemeval \
  /path/to/longmemeval_s_cleaned.json \
  --output /tmp/longmemeval-dry-run \
  --limit 5 \
  --dry-run
```

运行模块时，当前工作目录应为 `examples/agent_memory_v1`；也可以从仓库根目录把该目录加入 `PYTHONPATH`。

当前已下载并检查的文件是 `/Users/zsm/Documents/DB-GPT/data/longmemeval/longmemeval_s_cleaned.json`：约 265 MB、500 题，包含六类 `question_type`。发布文件的时间字段形如 `2023/05/30 (Tue) 23:40`，适配器已兼容该格式；清洗文件中少量空消息会跳过。全量 dry-run 已通过，尚未调用模型或生成正式分数。

## 小规模真实 pilot

```bash
export DASHSCOPE_API_KEY='…'
export AGENT_MEMORY_CHAT_MODEL='deepseek-v4.1-flash'
export AGENT_MEMORY_EMBEDDING_MODEL='text-embedding-v3'
export AGENT_MEMORY_EMBEDDING_DIMS='1024'

.venv/bin/python -m evaluation.longmemeval \
  /path/to/longmemeval_s_cleaned.json \
  --output /tmp/longmemeval-pilot \
  --limit 5
```

每题都写到自己的 `data/<question-id>/`，所以一题的历史或向量不会进入另一题。`records.jsonl` 是详细审计记录，`predictions.jsonl` 才是交给官方 QA scorer 的最小文件。失败题也会写空 `hypothesis`，不能因为跳过失败题而抬高分数。

LongMemEval 上游评分器提供任务类型对应的判分标准，但其论文汇总脚本固定使用 `gpt-4o-2024-08-06`。DashScope/Qwen 可以作为替代 judge：结果应标为“DashScope 自定义 judge”，不等同于官方 GPT-4o 分数，也不宜直接与论文结果比较。当前适配脚本是 `score_longmemeval_dashscope.py`，使用 Qwen 兼容 API、temperature 0，并保存逐题 YES/NO、指标和 manifest。

### 当前 DashScope 诊断评分

2026-10-12 已用 `qwen3.8-max` 对 11 道去重后的诊断样本评分：9/11（81.82%），六类题型平均准确率 80.00%，弃答题 1/1。样本是此前挑选的诊断题，不是完整数据集的随机/分层代表样本；弃答只有 1 题，因此这些数字不能视为系统总体准确率。分数、逐题判定和输入保存在 `data/longmemeval/runs/20261012_qwen3_8_max_judge11/`。复跑方式：

```bash
examples/agent_memory_v1/.venv/bin/python \
  examples/agent_memory_v1/evaluation/score_longmemeval_dashscope.py \
  --predictions data/longmemeval/runs/20261012_qwen3_8_max_judge11/predictions_input.jsonl \
  --dataset data/longmemeval/longmemeval_s_cleaned.json \
  --output-dir data/longmemeval/runs/20261012_qwen3_8_max_judge11 \
  --judge-model qwen3.8-max
```

脚本从 `DASHSCOPE_API_KEY`/`DASHSCOPE_BASE_URL` 或本机 DB-GPT Qwen profile 读取连接配置，不把 key 写入运行产物。任务标准参考 LongMemEval QA evaluator，但提示词和 API 调用由此适配器实现，因此该结果是自定义 judge 分数，不是原仓库的 GPT-4o 官方分数。

### 2026-10-13 分层 14 题 pilot

使用固定 seed `20261012`，从六种常规题型中各抽 2 题，再加入 2 道官方 `_abs` 弃答题；共 14 题、670 个历史 session。样本不与此前 11 道诊断题重叠。写入/回答使用 DeepSeek V4.1 Flash（thinking 开启）和 `text-embedding-v3/1024`；评分使用 DashScope `qwen3.8-max` 自定义 judge。

| 指标 | 结果 |
| --- | ---: |
| Overall Accuracy | 13/14（92.86%） |
| 六类题型平均准确率 | 91.67% |
| Abstention Accuracy | 2/2（100%） |

题型结果：`single-session-user` 2/2，`single-session-preference` 2/2，`single-session-assistant` 1/2，`multi-session` 3/3，`temporal-reasoning` 3/3，`knowledge-update` 2/2。唯一错误仍是 `7161e7e2`：回答没有编造 Admon 的周日班次，但记忆正文只保留“生成了一周轮班表”，漏掉了该员工的具体班次。来源 session 曾出现在 Top-10 候选中，因此此例指向提取/整理时丢失细节，而非来源检索未命中。

这是小样本 pilot，不代表 LongMemEval-S 全集或统计稳定的总体成绩；Qwen judge 也不是官方 GPT-4o 评分，不能与论文分数直接比较。运行输出、抽样清单和逐题 judge 结果见 `data/longmemeval/runs/20261013_deepseek_v41_flash_stratified14_qwen38_judge/`，抽样清单见 `data/longmemeval/selections/20261012_stratified14_seed20261012.json`。本次未安装 FastEmbed，因此 Mem0 BM25 关键词通道未启用；结果对应当前向量语义检索配置。

当前适配以 session 为一次写入单元；session 内消息保留 user/assistant 角色和 session 时间，最近十条消息由现有 service 作为整理上下文。问题、gold answer、answer_session_ids 和 has_answer 不会进入写入输入。
