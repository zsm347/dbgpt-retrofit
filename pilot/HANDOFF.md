# DB-GPT RAG 分块策略实验：当前交接文档

> 最后更新：2026-10-13

## 最新入口：采用B递归256/0；按用户决定暂时跳过保留题验证

流程变更记录：[WORKFLOW_DECISION_2026-10-13.json](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/WORKFLOW_DECISION_2026-10-13.json)。这是用户明确要求的暂时跳过；不记为已验证。原selection_decision_v1保留当时的选择依据和后续计划，执行顺序以本次变更为准。

**最新选择：**复用全部18配置的现有证据字符P/R/IoU、原始Ragas和资源记录，将 **B_recursive、chunk_size=256、overlap=0（本地cl100k_base tokens）** 选为当前冻结语料的检索默认。依据见[选择报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/selection_decision_v1/report.md)，完整36行指标见该目录metrics.csv/json，控制条件见[selected_controls.json](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/selection_decision_v1/selected_controls.json)。B/256/25的预算字符P/R/IoU均值略高；B/256/0分别低0.0216/0.0676/0.0232个百分点，选择无重叠作为较简单默认，并参考它的top-5 IoU较高。没有证明两者等价或B统计占优，未称全流程最优。

2026-10-12 的中文网格 `paired_chunking_grid_v3` 已完成 9/9 个参数运行、18/18 个策略身份：1,656 份回答、6,624 项 Ragas 主评分，最终技术失败 0。2026-10-13 的完整性检查、四项指标分开汇总、事实组配对 bootstrap、参数/题组敏感性、调用历史和原始 judge 审计已完成。

- 当前入口：[运行与分析说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/RUN.md)、[主分析报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/analysis_v1/report.md)、[judge 审计](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/analysis_v1/judge_audit.md)。
- 实际知识库是 29 份冻结中文 Markdown；调参题来源于其中 23 份，共 23 个事实组、46 个法规名称显式/省略问法。holdout 问题未用于这次调参或分析，其六份来源文档已入索引，后续不能称为未见文档验证。
- 本轮实际为 18 套不同块集合；旧语料的“18 个身份、16 套块”不能套用。
- 回答和 judge 均为 `deepseek-v4.1-flash`、思考关闭；Embedding 为 `text-embedding-v3`。主要比较 2560 本地 token 预算，top-5 单独报告。主实验仍使用四项 Ragas 内置指标；2026-10-13 应用户询问，补算普通文档级 P/R，作为单独来源诊断，不恢复 AnswerAdequacy 或改写主指标排名。
- 原始自动 F1 的重点复核对象是 C/512/0（72.20%）和 B/256/51（70.57%）；差 +1.63 个百分点，配对区间 [−5.96, +8.96]。不能据此宣布最终优胜。
- 已复现引用惩罚：答案完整包含参考答案并加上原文真实条号，judge 将条号复制进每条声明，再因 reference 不含条号把所有正向声明判为 0。62 条机械异常记录来自同一事实组的配置/问法/视图重复，不是 62 道独立题。原始评分未改写。

**当前安排：用户明确要求先跳过下一步验证，7个保留事实组/14问法暂不运行。继续采用B/256/0作为当前默认，下一步转向固定分块后的检索top-k选型。** 保留题尚未读取，运行配置尚未创建。逐题漏召回复盘为可选解释分析，不是选型前置；已有证据标注不因排名改写。原始事实F1异常尚未校准，当前只作辅助展示；AnswerRelevancy仍未计算且为可选。主视图仍为2560本地token预算，top-5单报。当前是检索默认的调参选择，未完成独立确认，未修改线上DB-GPT配置。第8组技术恢复历史继续保留。

**2026-10-13 检索优先修订与离线补算：**核对开源 `chunking_evaluation` 后，不再把事实F1校准及全18配置AnswerRelevancy补算作为分块检索选型前置。当前流程见[检索优先选型协议 v2](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/RETRIEVAL_SELECTION_PROTOCOL_v2.md)。已对全部18配置、两视图、1,656条逐题记录补算证据字符IoU，保存36组汇总，原块/字符P/R全部一致；公式为 `covered_gold_chars / (retrieved_chars_including_overlap + missing_gold_chars)`。预算视图 B/256/25 的字符P/R/IoU为10.79%/95.94%/10.70%，B/256/0为10.77%/95.87%/10.68%，C/256/51为10.82%/92.76%/10.67%；微小IoU差异不直接裁定胜者。结果见[18配置检索比较](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/retrieval_selection_v1/report.md)，重现脚本 `pilot/rag_pipeline/compare_evidence_retrieval.py`。未调用模型、未读取保留题、未改写原始结果。随后已形成当前默认B/256/0，详见最新选择报告；逐题漏召回复盘为可选解释，未改gold。

**2026-10-13 会话初版设计（执行顺序已由v2更新）：**初版记录见[选型、固定配置与保留题验证协议](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/SELECTION_AND_VALIDATION_PROTOCOL_2026-10-13.md)。主要比较继续使用共同2560本地token预算，固定top-5单独报告。23个事实组/46个问法用于比较18配置并选型；7个事实组/14个问法用于对已选定配置作初步确认。调参阶段可以整理少量候选，但验证前应选定一个主配置；保留题不再承担挑选18配置的工作。验证前固定数据/评分版本、模型、分块、检索、提示词和判读规则；根据保留结果调整方案后，该批题即成为已参与开发的数据。

**AnswerRelevancy 初版安排与当前状态：**原规划为18×46=828条预算视图既有回答的辅助补算，复用 `user_input`/`response`，新增评审与embedding调用。当前尚未执行，按v2调整为候选整体RAG检查中的可选补充，不作为检索选型前置。若补算，所有参与同一比较的候选须使用一致设置。各项指标分别比较，不简单平均。普通块/证据字符P/R已覆盖所有18配置、两个视图，现又补充字符IoU；先前只展示两代表不等于只计算两代表。top-k优化另作后续实验。

离线重现：`pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/analyze_paired_chunking_grid.py`。输入哈希与来源校验见 `analysis_v1/integrity.json`，下一阶段决策见 `analysis_v1/decision.json`。

**普通 Precision / Recall 补充（2026-10-13）：**已基于现有 gold 来源文档 ID 和实际进入模型上下文的块→文档映射，计算所有 18 个身份、两个视图及两问法的文档集合 P/R，共 1,656 条逐题结果、108 行汇总。不依赖 LLM；同一文档重复块只计一次。公式为 `P=|G∩R|/|R|`、`R=|G∩R|/|G|`。预算视图 C/512/0 为 P=56.16%、R=100%，B/256/51 为 P=47.86%、R=100%；宏平均先合并事实组内两问法。本次每题仅标注一个来源，所以 Recall 等于来源命中率，不能说明召回了其中正确条款；其他来源也可能有未标注的替代证据。文档 P/R 不能当作块 Precision@5、证据覆盖率或回答准确率，也不能仅用此指标选策略。详情见 [普通文档级 P/R 报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/source_document_metrics_v1/report.md)，可用 `pilot/.venv-rag-eval/bin/python pilot/rag_pipeline/compute_source_document_metrics.py` 重现；原 Ragas 评分与四指标分析保留，未调用模型、未读取留出题。若要评测证据级普通 P/R，下一步应以原文条款等固定单元核对必需/替代证据与相关性规则，再统一映射所有候选。

**证据到块的映射已启动并完成初步计算（2026-10-13）：**已有 gold span offsets 已逐项核对 canonical 原文，并映射到全量候选块与实际召回块。23 个事实组共享配对问法；独立证据区间 29 个，问题记录中的区间 58 条。另存了“任意字符交集”下的块 P/R、证据字符 P/R、完整 span 覆盖率；采用 1,656 条题目/视图记录，未调用模型、未读取 holdout。预算视图 B/256/51 的交集块 P/R=17.19%/95.43%、证据字符 P/R=10.56%/94.89%；C/512/0 为 19.12%/86.96%、9.13%/88.22%。此处“块相关”是自动的位置交集判定，可能把擦到证据边缘的块也算相关；28/29 独立 span 粒度为整条 article。C/512/0 有一题来源文档召回100%但560字符证据区间召回0%，表明来源 ID P/R 不足以评价分块。结果采用位置标签，结论限于现有标注口径；没有具体标注错误时可固定gold直接比较，逐题复盘为可选解释分析。详情与逐块 ID：[证据映射报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/evidence_chunk_mapping_v1/report.md)，[复现脚本](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/map_evidence_to_chunks.py)。

## 历史说明（以下早期“当前/下一步”以最新入口为准）

> 当前状态：全 24 题初始比较 v4 已完成，96 份回答/480 项评分，最终技术失败 0。18 个候选身份对应 16 种不同分块结果，网格 v4 已完成；每组 48 条回答、全部指标就绪、技术错误 0。配对分析、coverage sanity 和代理回源复核已保存到 `m4_grid_analysis_v4/`。QA v3、criteria_v2、并发 4 和统一输出截断恢复已登记；实际自动评分仍存在关系/归因漏判和拒答漏判，人工审核 pending。当前入口见 M4_RUNBOOK。  
> **已有完整网格的探索性模型结果；最终策略与参数仍需人工裁决和独立材料确认。**

> **当前工作更新：中文 Markdown 语料已完成面向 RAG 的质量审核、最小清洗和 canonical 冻结。** 基于29份冻结文档生成的30条 Ragas 候选已逐题对照原文：26条可保留为候选，4条修订已写入语义审核版；七条款长题单独作为复杂覆盖题。另构造30组成对问法（60条变体），仅用于诊断法规名称线索的影响，每对共享答案和证据，因此只有30个事实/证据组。逐对初审时修正了女职工题中不自然的“工作场所期间”措辞。已按文档分组并固定探索性 QA 实验集：调参集23份文档/23个事实组/46个问法，留出集6份文档/7个事实组/14个问法；同一文档、同一事实的配对问题没有跨组。留出集样本较小，只能作初步确认，不是最终 benchmark。当前尚无基于该新实验集的 B/C 分数。**下一步仅用调参集跑小规模 B/C 配对诊断，分别报告法规名称显式/省略结果；配置确定后再查看留出集。** 详见[探索性实验集说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/title_ablation_v1/experiment_set_v1/README.md)、[标题线索消融审核包](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/title_ablation_v1/review_packet.md)、[语义审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/semantic_review_v1.md)和[冻结语料审核报告](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/audit_report.md)。本轮核对冻结语料中的问题、答案和证据，不认证法律效力或外部版本。

> **QA生成来源更正（2026-10-12）：**旧44条QA由Codex代理依据原文编写，脚本整理、代理复核后取24条用于M4；没有执行Ragas官方`TestsetGenerator`，Ragas实际用于后续评分。新一轮先完成3份文档的3条适配小批，随后完成全29份文档的30条 v3 候选生成和条款级回源检查；两批均尚未人工批准或冻结为正式 QA。证据与边界见[核对说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/QA_GENERATION_PROVENANCE_2026-10-12.md)、[v3审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/review_packet.md)及[小批审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v1/review_packet.md)。

> 离线诊断更正：原 TF-IDF 百分比存在来源映射错误、全英文问题与部分中文文档的语言失配，以及覆盖区间重复计数问题。仅保留为历史脚本输出，不用于策略排序或表格能力判断；详见离线诊断对照报告。

## 0. 阅读入口与项目目标

详细记录见：**[RAG_CHUNKING_EVALUATION_PLAN.md](/Users/zsm/Documents/DB-GPT/pilot/RAG_CHUNKING_EVALUATION_PLAN.md)**。

该文档包含当前语料审查、评测集建立方式、字段来源、开源方案比较、指标定义、参数搜索、实验公平性、结果记录、实施里程碑和官方来源。

工作目录：`/Users/zsm/Documents/DB-GPT/pilot/`。

最初背景是围绕 DB-GPT 做 RAG 方向的项目改造与实证分析，用于形成可说明、可复现的项目经验。当前主线是：**用固定、经过审核的真实文档和问题，确定分块策略及参数。** 后续项目说明应区分已经运行的结果、历史探索和待实现方案。

## 1. 已确认决策

1. **主实验移除 A 固定长度，只比较 B/C。** `chunkers.py` 中 A 函数和 `STRATEGIES` 注册仍存在；runner 必须显式选择 B/C。
2. **第一阶段研究正文分块，暂不考虑代码块和表格专项。** 用户明确表格不是重点，暂不增加表格保护/行组分块/表头重复机制，9 道表格题不参与首轮评分。围栏图示也可能被原统计归入代码块；行内代码另属统计口径。
3. **策略与参数需要通过实验确定。** 当前默认 `800/100` 是字符参数，是历史实现设置，不是选型结果。
4. **当前推荐现有 B/C + Ragas + 项目实验编排。** 复用 QA 生成、schema、评分和公开模板，补充当前链路的 adapter 与 runner。
5. **不再以 `answer[:60] in chunk.content` 作为主命中判定。** 参考答案可以改写，支持证据必须真实、充分、可追溯；需要语义评测或可靠来源映射。
6. **粗搜候选已制定，最终取值未确定。** 当前 v1 为 B/C × 256/512/1024 个本地 tokens × 0/10/20% overlap，向下取整，共 18 个配置/16 组独立块集合；详见 `M4_PROTOCOL.md`。这些是实验候选，不是行业标准或最优参数。此前 400/800/1200/1600 字符建议不作为本轮配置。
7. **正式实验采用 token 作为块大小与重叠单位。** 用户已接受该方向。M3 固定 `tiktoken 0.9.0 / cl100k_base` 作为本地计量器，接入当前活动 `qwen` 配置的 `text-embedding-v3`；两者不是同一个 tokenizer。M3 size=512、overlap=51，只是起始参数。标题和来源标签分别计入最终块长及上下文预算；另保存 provider 用量与输入限制检查。旧 `800/100` 字符运行只作历史记录，不能当作 `800/100` token。
8. **长度计量必须显式记录。** `pilot/rag_pipeline/token_measure.py` 提供 `characters`、`tiktoken` 和 `transformers` 三个后端；token 后端需要明确 tokenizer/encoding，依赖或标识缺失时直接失败，不会静默退回字符数。C 已扣除标题前缀预算；字符回归符合上限。token 计数不保证可相加，正式 runner 仍需对最终拼接文本逐块检查长度，不能仅凭“上限减前缀”宣称一定合规。
9. **先扩充真实Markdown正文语料，旧结果保留为探索。** 不为了压低分数制造难题，也不因长文就假定基础RAG必然低分。新QA需要覆盖真实条件/例外、相似条款、必要的多证据组合和不可回答题，并登记无检索上下文对照及可靠来源检查。暂不恢复表格、代码、图片专项。

阿里云官方接口会在 embedding 响应中返回 `usage.total_tokens`，这是 provider tokenizer 的计数，可用于单文本审计；批量响应是整个请求的总数，不能平均分摊到每个块。已完成 4 份接口验证和后续 14 份纳入文档审计，仅保存计数和哈希，没有把 `text-embedding-v3` 的 tokenizer 假定为 `cl100k_base` 或 Qwen tokenizer。

Ragas 0.4.3 已安装在独立环境 `pilot/.venv-rag-eval`，已接入真实评分。M4 v4 的全 24 题初始比较和 18/16 配置网格均已完成；配对分析、coverage sanity、失败清单和代理回源复核已保存。独立人工审核、新保留材料确认仍待完成。

## 2. 当前进度

当前中文网格阶段：

| 工作 | 状态 | 说明 |
|---|---|---|
| 29 份中文规范文本冻结与索引 | 已完成 | 调参问题来源 23 份；完整知识库进入召回 |
| 9 个参数运行、18 个策略身份 | 已完成 | 18 套不同块，1,656 份回答、6,624 项主评分 |
| 分组汇总与配对分析 | 已完成 | 23 个事实组；两问法、两视图分别记录 |
| 自动案例及原始 judge 审计 | 已完成 | 引用前缀惩罚已复现，不改写原始分数 |
| 证据字符IoU及统一检索比较 | 已完成 | 18配置×两视图，1,656逐题记录、36组汇总 |
| 候选必要证据范围及检索差异核对 | 可选解释分析 | 保留现有gold，未因排名重标；不是选型前置 |
| 统一回答评分校准 | 待实施 | 在候选回答比较使用事实F1前处理已知异常 |
| AnswerRelevancy 辅助指标补算 | 后续可选 | 828条原规划尚未执行，不再作为检索选型前置 |
| 调参集选型及记录控制条件 | 已完成 | 检索默认B/256/0，选择为事后工程取舍，不证明全流程最优 |
| 留出问题确认 | 按用户决定暂时跳过 | 尚未读取或运行；当前结论基于调参集 |
| 检索top-k选型 | 下一步，尚未开始 | 固定B/256/0，复用现有调参问题 |

下表保留旧 M4 与早期准备阶段的历史情况：

| 工作 | 状态 | 说明 |
|---|---|---|
| Markdown 历史筛选 | 已完成 | 212 份扫描记录，195 份保留快照 |
| Markdown 规则清洗 | 已完成 | 不等于所有文档已经人工质量审核 |
| 分块器实现 | 已有 | B/C 可复用；A 仍在代码中但退出主实验 |
| Word 解析探索 | 已有 | 16 份自定义解析产物、MarkItDown 示例 |
| PDF 解析探索 | 已有 | 21 份已转换，含批量摘要与表格审查 |
| 公开评测方案调研 | 已完成一轮 | 详细来源见方案文档 |
| 第一阶段语料冻结 | 已完成（暂定 v1） | 14 份纳入、14 份暂缓；见 `pilot/data/eval/corpus_manifest.json` |
| 语料冻结审核报告 | 已完成（文本层） | 见 `pilot/data/eval/_corpus_review.md` |
| QA 候选与代理初审 v1 | 已保存历史版 | 44 条、覆盖 14 份；后续使用 v2，避免混合版本 |
| QA 逐题回源复核 v2 | 已完成代理复核 | 内容保留 15 题、修订 29 题；31 英文、13 中文；9 条表格题；57 个支持证据区间 |
| 首轮正文评测范围 | 已按用户决定另存 | `qa_text_only_v2.jsonl`：35 题、27 英文/8 中文、调参 24/原保留 11；父集 44 题保留，筛选规则和哈希见对应 manifest |
| QA 人工审核与最终冻结 | 待做 | 阅读 `qa_review_packet_v2.md`；当前状态为 `agent_source_reviewed_pending_human_review` |
| 正文调参参考核对 v3 | 已完成代理核对 | 24 题、19 英文/5 中文、64 条必答事实；另存 `qa_text_tuning_v3.jsonl`，问题/ID/split/原文不变，父集保留 |
| Judge 控制诊断 | 已实际运行 | 21 例、两套按题提示词各 21/21 与代理控制一致；严格拆解变体仍漏步骤关系，人工对齐待完成 |
| 调参集/保留集划分 | 已完成暂定版 | 按完整文档划分：30 条调参、14 条保留；见 `qa_splits.json` |
| 离线批量检索诊断 runner | 已实现并运行 | TF-IDF 仅用于链路诊断；见 `pilot/rag_pipeline/evaluate_chunking.py` |
| Ragas 适配与评分 | 已接通 M3 | `ragas_adapter.py` 负责字段转换；`run_model_eval.py` 使用 Ragas 0.4.3 的四项官方指标，保存原始 judge 判断 |
| 长度计量入口 | 已实现；M3 固定本地口径 | `token_measure.py`；M3 使用 `tiktoken 0.9.0 / cl100k_base`，最终块长检查通过；不冒充 provider tokenizer |
| token 后端冒烟 | 已完成（代理） | `cl100k_base` 仅验证计量接线；不是 `text-embedding-v3` 的正式 tokenizer，结果不用于策略排序 |
| provider token 审计 | 已完成首轮全量 | 14 份纳入文档逐条调用 `text-embedding-v3`，共 12,128 provider tokens；见 `provider_token_audit_full_v1.json`；4 份小样本仍保留为接口验证记录 |
| 最小模型评测 runner | 已实现并实际运行 | `run_model_eval.py`：embedding → 精确余弦检索 → 回答 → Ragas → 保存；M3 4 题、16 份回答、64 项评分 |
| M3 配置与代理逐题检查 | 已保存；人工验收待完成 | `m3_text_smoke_v2/`；v1 保留为参考答案指代/网络故障诊断，不能混合两版分数 |
| B/C 离线检索诊断 | 已运行字符回归 | QA v2、800/100 Python 字符、top-k=5；B 93.18% / 90.23%，C 97.73% / 93.77%；C 为标题预算修正后的 116 块，仍不是最终 LLM 效果结论 |
| 离线诊断对照报告 | 已完成并区分历史/修复后结果 | [diagnostic_comparison.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/diagnostic_comparison.md) |
| 参数粗搜索、细搜索 | 网格 v4 已完成；正式确认待做 | 18 配置/16 独立块集合，每组 48 条回答；原始自动评分、coverage sanity、失败项和代理回源复核见 `m4_grid_analysis_v4/` |
| 最终推荐配置 | 未确定 | B/512、overlap 51/102 是当前候选；必须经人工裁决和独立材料确认后再定案 |
| 新Markdown语料筛选 | 已完成初筛 | 固定版本2,729份文件结构统计及哈希检查；29份中文/76份GitLab主体候选，24份Dwarves备选，见新筛选报告 |
| 新语料规范文本 | 已完成 canonical v1 | 29 份中文候选通过结构检查；见 `pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/`。法律当前效力仍需按题目做官方正文核验 |
| 新 QA 生成与审核 | v3 候选已生成，正式集待做 | Ragas `TestsetGenerator` 0.4.3 已从29份文档生成30条候选；哈希、来源和6条多证据题的条款区间已脚本化核验，逐题语义抽查、题型补充和 QA 冻结仍待完成 |

当前已完成语料筛选、QA 代理回源复核、来源映射/计量/provider 审计、M3 最小闭环，以及 M4 的全调参集参考核对、judge 构造控制、完整 18/16 配置网格和配对分析。人工 QA/候选回答审核、judge 独立人工校准和新的保留材料确认仍待完成。coverage sanity 已发现部分拒答会被自动 judge 漏判，不能把原始自动均分直接当作策略优劣。provider token 与 `cl100k_base` 不同，本地口径和模型用量分别记录。M3 四题及构造的代理控制、M4 调参集结果均不构成最终业务可靠性结论。

## 3. 语料清点：使用当前修正口径

### 3.1 历史 Markdown 快照

位置：`pilot/data/rag_corpus/project_docs/cleaned/`。

- 原清单保留 195 份，清洗后共 666,117 个非空白字符。
- 195 表示已有历史快照，不是当前源目录所有 `.md/.mdx` 的实时总数。
- 原清洗记录：删除 HTML/JSX 标签 1,814 处、JSX 表达式 308 处、MDX import 49 行、容器标记 196 处，保留 1,039 个围栏块。
- 清洗规则可能移除图片引用或其他 HTML 结构，使用前仍需回源审查。

按原 manifest 分类：

| 类别 | 份数 |
|---|---:|
| 无围栏代码、无规则识别表格 | 21 |
| 无围栏代码、有规则识别表格 | 7 |
| 有围栏代码、无表格 | 112 |
| 有围栏代码、有表格 | 55 |
| 总计 | 195 |

**第一阶段无围栏代码候选共 28 份，清洗后 47,749 个非空白字符。** 62 份含表格文件中有 55 份同时含代码，不能直接把 21+62 当作无代码实验语料。

28 份候选已经完成首轮文本层筛选：14 份暂定纳入、14 份暂缓。纳入文档清洗后共 36,265 个非空白字符。具体名单、哈希、纳入/排除理由见 [_corpus_review.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/_corpus_review.md) 和 [corpus_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/corpus_manifest.json)。

这不是最终 QA 质量认证：仍需逐题核对事实、表格行列、版本冲突和图片依赖。纳入文档含图片引用的，只生成依据文本明确内容的问题。

### 3.2 语言和代表性

不能根据 `zh-CN` 目录认定文件是中文。当前无代码候选以英文技术文档为主；按 Han/(Han+ASCII 字母) 粗略指标，28 份中仅 2 份达到 50%。这个指标只用于筛查，不是人工语言质量认证。

旧交接文档中的“119 份占比低于 20%、28 份真正中文”等说法与已有统计口径不一致，不再使用。

现有候选可用于流程试跑；如果正式目标是中文企业制度/SOP，需要补充同领域中文语料和业务问题，或明确结论只针对当前技术文档子集。

### 3.3 Word

- 来源清单：[_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/institution_docs/_manifest.json)。
- 16 份 Word 已有 `pilot/parsed/word/` 的 Markdown 和 JSON sidecar。
- 15 份含表格；《山西农业大学规章制度管理办法》未检出表格。
- 有采购文件、管理制度、通知，也有申请表、自查表和制度汇编目录。
- 正文较丰富文件可作为中文补充候选；短表单、目录不宜默认作为常规 QA 主语料。
- 自定义编号标题识别可能过度提升普通条款，需要人工审核层级。

**撤回旧断言：**不能仅以“只有 16 份”判定无法做统计实验。应考察独立问题数、内容覆盖、重复程度和得分方差。

解析报告：[_parse_report.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/word/_parse_report.md)。

### 3.4 PDF

- 21 份已通过 MarkItDown 0.1.7 转换，合计 5,506 页，输出约 17.8 MB Markdown。
- `pilot/parsed/markitdown_pdf/batch/` 保存全部转换产物。
- 普通文本保留较充分；字符二元组覆盖指标不表示表格、标题、阅读顺序正确。
- 输出未形成 Markdown 标题层级和图片引用。
- 复杂财务表格有错列、断行、表头丢失、数字拆开和伪表格等问题；部分小型简单表格可读。
- 目前有批量机器诊断和代表性抽查，没有所有表格逐个完成的人工正确性认证。

因此，旧“PDF 未解析”已过时。当前 PDF 不适合直接作为结构分块主实验输入，解析结构与表格改进应单独处理。

报告：[batch_summary.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/markitdown_pdf/batch_summary.md)、[batch_table_audit.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/markitdown_pdf/batch_table_audit.md)、[eastmoney_table_audit.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/markitdown_pdf/eastmoney_table_audit.md)。

### 3.5 新Markdown长文候选（2026-10-10）

新快照独立保存于 `pilot/data/rag_corpus/candidate_screening_2026-10-10/`；旧14份语料、QA和M4结果未改写。

| 候选 | 数量 | 正文总本地tokens | 中位/最大 | 使用边界 |
|---|---:|---:|---|---|
| lawtext企业合规 | 29 | 286,552 | 9,183 / 37,282 | 中文法规，来源是官方DOCX的第三方Markdown整理；22份有ATX标题，无规则检出的围栏/表格/图片；仓库状态及官方全文仍待逐份核对 |
| GitLab员工政策/流程 | 76 | 286,410 | 2,652 / 11,592 | 英文真实企业手册；28份表格、18份Hugo短代码、29份H4/H5；部分事实依赖外链/模板，不可直接作为完整已清洗语料 |
| Dwarves备选 | 24 | 26,137 | 846 / 3,501 | README明确CC-BY-4.0，但偏短且仓库已归档 |

计量为 `cl100k_base` 的结构筛查视图，排除YAML/HTML注释、保留Markdown和模板；不是provider tokens，也不是已经形成的规范文本。29份中文按中文文献名保存了字节一致的可读副本。候选未生成新QA，不构成开源benchmark。来源许可证据、重复版本/转载、下载范围及具体暂缓理由见[报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/corpus_screening_2026-10-10/report.md)。

中文优先可先处理29份合规候选；企业SOP目标下GitLab更贴近业务，但应单独评测，不将两套分数混成一个总体分。尚未找到同时满足中文、真实企业SOP、大规模长文及清楚内容许可的完整公开库。

### 3.6 中文合规语料规范化冻结（2026-10-12）

已对中文候选 29 份执行结构性审核和最小清洗，产物目录为 [`zh_enterprise_compliance_canonical_v1`](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1)。冻结清单是 `canonical_manifest.json`，逐文档报告是 `audit_report.md`。

- 29/29 份 raw SHA-256 和 Git blob SHA 与固定 lawtext commit 匹配；正文非空、UTF-8 解码正常、没有未闭合围栏。
- 删除 12 个明确的目录导航块；没有发现 HTML 注释。没有添加任何标题，没有改写条款，没有合并版本或同名替代来源。
- 规范正文共 284,629 个本地 `cl100k_base` tokens（原正文视图 286,552）；这只是本地计量，不是 embedding provider tokenizer。
- 每篇都有 metadata sidecar 和 raw→canonical 字符区间映射；规范文本结尾的合成换行不属于可引用证据区间。
- 29 个 NPC 数据库链接于 2026-10-12 均返回 HTTP 200，但内容是前端应用壳，不等于已读到官方正文；仓库 `status: 有效` 也不等于当前法律效力认证。

因此当前状态是 **canonical frozen, pending QA**：可以用这批固定文本生成候选问题，但不能把它写成已完成法律版本核验或已完成评测集审核。

## 4. 当前分块代码：哪些能力可用、哪些仍需改进

源码：[chunkers.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/chunkers.py)。

| 组 | 实现 | 当前实验地位 |
|---|---|---|
| A | `CharacterTextSplitter(separator='')` | 历史实现，退出主实验 |
| B | `RecursiveCharacterTextSplitter` | 保留候选 |
| C | `MarkdownHeaderTextSplitter` + 递归二次切分 + 标题注入 | 保留候选 |

默认 `chunk_size=800`、`overlap=100`，单位为 Python 字符长度。

C 的现有机制：

1. 按 H1/H2/H3 切出章节，标题进入 metadata。
2. 对章节递归二次切分，继承 metadata。
3. 把标题路径注入文本，参与当前文本向量化。

现有限制：

- H4 及更深标题未登记。
- B 未显式增加中文句号等分隔符。
- 表格没有专门完整保护。
- 旧讨论中的碎块分类合并尚未实现。
- C 字符标题预算已修正；正式 token 路径仍需核对最终拼接文本，不依赖 token 长度可相加的假设。
- 实际重叠受章节和分隔符影响，不一定等于配置值。
- `locate_spans` 现在按空白归一化定位，找不到时返回 unresolved，不再构造估计区间；旧映射回归的 B 74/74、C 113/113 均定位成功，标题预算后的最新字符回归为 B 74/74、C 116/116；重复文本、更多参数和 C 标题证据仍需继续审计。

严格证据覆盖现在先对 top-k 分块区间取并集，再与 gold `source_spans` 求交集；无法定位的内容标成 unresolved，不计入严格覆盖。C 的注入标题目前不计入原文正文区间；可先用 Ragas 语义指标开展主评测。

## 5. 推荐的评测方案

### 5.1 QA 建立

- 从冻结文档出题，不为每个分块配置重新生成不同 QA。
- 采用真实问题、人工编写和生成器候选相结合；当前没有确认可用的真实查询日志。
- 参考答案允许准确归纳；证据必须是源文档中真实支持答案的内容。
- 正式纳入的问题逐题审核，特别检查表格行列、单位、脚注和多处证据。
- 保留问题类型、来源版本和审核记录，划分调参/保留集。
- 不以任意统一题量或答案字数作为行业要求。

Ragas 核心字段：`user_input`、`reference`、`reference_contexts`，运行时加入 `retrieved_contexts`、`response` 等。`doc_id`、offset、页码、题型、版本、审核状态是项目补充元数据，不能宣称全部属于 Ragas 标准 schema。

### 5.2 指标

当前建议：

| 层次 | 指标 | 注意事项 |
|---|---|---|
| 检索 | LLM Context Recall | 参考答案主张获得上下文支持的程度，不是字符覆盖率 |
| 检索排序 | Context Precision | AP 类有用块排序指标，不是有用字符比例 |
| 回答正确性 | Factual Correctness | 明确使用的模式和版本 |
| 回答忠实性 | Faithfulness | 高分不代表答案完整 |
| 运行 | token、延迟、调用数、成本、失败数 | 与质量一起报告 |
| 可选增强 | 可靠原文区间覆盖 | 最新字符诊断已修复正文映射与区间并集；其他参数、标题证据与裁剪后上下文仍需审计 |

Ragas 评分需用统一 judge 设置，并与有代表性的人工判定核对。LLM 分数不是人工真值。

### 5.3 公平比较与搜索

- 固定语料、QA、embedding、retriever、回答/评审模型及提示词。
- 同时观察固定 top-k 和固定上下文 token 预算的结果。
- 明确最终送入模型的上下文、去重与截断规则。
- B/C 都保留合理参数候选；初始一个参数点的输赢不作为最终淘汰依据。
- 根据实际段落/章节/token 长度、预算和初始结果制定粗搜索，再局部细化。
- 参数单位清楚，不能把 Azure 的 token 建议直接当中文字符数。
- 质量接近时看逐题差异、波动、成本及错误类型，不仅看平均分小数位。

完整指标定义、来源与执行流程见详细方案第 8–18 节。

### 5.4 候选筛选与固定设置：2026-10-09 工作校准

“候选”是完整分块配置，例如 `B + 512 本地 tokens + 10% overlap`；不是只在 B/C 两个名称之间二选一。“较好的候选”原表述没有配套筛选规则，不能据此自动淘汰配置。

- 检索比较使用同一 embedding 的语义检索结果，报告 Ragas 的参考答案支持率（Context Recall）、有用块排序（Context Precision）、实际上下文长度和逐题退步；TF-IDF 数字与严格字符覆盖仅作诊断。
- **当前建议：首轮小网格对所有有效且不重复的候选完成检索与回答评测。** 若以后因成本改为先筛检索、再评回答，应在运行前写明保留规则，对 B/C 均保留候选；不能事后只挑均分第一名，也不能把检索领先等同于回答领先。
- `B/C × 256/512/1024 本地 tokens × 0/10/20%` 已完成 v4 网格：18 个声明配置、C 的 1024 三种重叠完全等价、16 组独立块集合。配置身份全部保留，等价计算复用；每组 48 条回答，结果和覆盖诊断见 `m4_grid_analysis_v4/`。
- 固定模型是选择可用、支持中英文且成本可承担的控制环境，再统一使用。M3 已接入 `text-embedding-v3`（1024 维），回答和 judge 均为 `qwen-plus-2025-12-01`；接口可用，judge 与人工判定的一致性仍待确认，这些模型没有被证明最优。
- **后续新一轮模型已切换：**当前 `/Users/zsm/.dbgpt/configs/qwen.toml` 登记的 LLM 是 `deepseek-v4.1-flash`，embedding 仍是 `text-embedding-v3`。新的 QA 生成、回答和 judge 统一使用 DeepSeek；旧 M3/M4 的 qwen 结果及 QA smoke v1 的 qwen3.8-max 结果保留为历史，不能混用。具体边界见 [模型设置说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/MODEL_POLICY_2026-10-12.md)。
- M3 已固定精确余弦语义检索、`top_k=5`、全语料检索，不启用查询改写、混合检索、rerank 或邻块扩展，不按 gold `doc_id` 过滤。另报告 2560 个 `cl100k_base` 本地 tokens 的预算视角：前 20 个候选按排名装入完整块，包含标签，放不下则跳过，不裁剪。具体设置是可复用的实验起点，不是最优检索结论；M4 运行前仍需登记搜索协议。
- 配置必须保存模型标识/可用版本、维度、归一化、提示词、采样参数、输出限制、Ragas 版本及具体指标、计量器、去重/截断、重试和缓存规则。服务只有可变模型别名时，记录返回的标识和运行日期，不声称完全锁定了模型权重。

这是本次复核后的实施建议，未将未验证的选项登记为用户已确认决策。详细规则见方案第 11.6、13.5 节。

### 5.5 用户最新范围决定：首轮只评正文分块

用户明确“表格不是重点，可以先不考虑”。当前 B/C 没有识别表格元素或保护表头/表体的专门逻辑，表格只是普通 Markdown 文本。执行范围现为：

- 使用 [qa_text_only_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_text_only_v2.jsonl) 的 35 道非表格题，M3/M4 取其中 24 道调参题；11 道原保留题仍已暴露，最终验证需要新的独立材料。
- [qa_text_only_v2_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_text_only_v2_manifest.json) 保存父集/子集哈希、选择条件、纳入/排除 ID、语言和 split。保留题逐行原样复制，QA ID、答案、证据、审核状态与文档划分不变；原 44 题及审核产物保留。
- 14 份语料和原文哈希保持现有版本，表格文本留在文档内照常切分和索引。这里只排除表格任务，不把语料称为纯无表格文档。
- 首轮不实现表格专用分块、不以表格题调参、不补充表格题、不声称本轮验证了表格能力。评分分层使用正文题型和语言。
- M3/M4 显式选择该子集及 tuning split。历史 TF-IDF runner 默认仍读取完整 v2 并运行全部 split，不能直接当作本轮正式评测入口。

## 6. 历史M1–M4实施与后续工作

**本节M1–M4保留历史范围。当前新语料工作优先读取3.5及语料筛选报告；新文本/QA审核完成后再登记实验协议，不沿用旧自动排名直接定案。**

### M1：冻结第一阶段语料（已完成暂定 v1）

已审核 28 份无围栏代码块候选的文本内容和结构，暂定纳入 14 份、暂缓 14 份，记录内容哈希、标题/表格信息、图片引用和纳入/排除理由。中文 Word 是否补充需按正式目标决定，并先审核解析。

产物：[corpus_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/corpus_manifest.json)、[_corpus_review.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/_corpus_review.md)。

### M2：生成并审核固定 QA（已完成 v2 回源复核，人工审核待做）

44 条 QA 由 Codex 代理依据冻结文档编写，不是人工编写或真实用户日志。已完成逐题回源复核，内容保留 15 题、修订 29 题：替换重复事实题、补齐表头、收紧证据、补全答案、限定冲突文档范围，并将中文文档的 13 条题改为中文。原 seed/provisional 保留，v2 状态为 `agent_source_reviewed_pending_human_review`，不能冒充人工通过的最终 gold QA。

当前产物：

- [qa_review_packet_v2.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_review_packet_v2.md)：44 题的问题、答案、原文、来源位置、判断理由和修改前内容，可直接阅读。
- [qa_source_reviewed_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_source_reviewed_v2.jsonl)：修订 QA、来源哈希、57 个支持证据区间、52 个参考上下文区间、语言及审核状态。
- [qa_audit_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_audit_v2.jsonl)、[qa_review_decisions_v2.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_review_decisions_v2.json)：逐题修订记录及可重建的决策输入。
- [qa_quality_report_v2.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_quality_report_v2.md)、[qa_version_manifest_v2.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_version_manifest_v2.json)：覆盖、机器校验、限制、版本与产物哈希。
- [ragas_input_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/ragas_input_v2.jsonl)：字段转换，未评分。

仍按完整文档分为 30 条调参、14 条保留，但原保留题已经用于历史 TF-IDF 诊断，因此不再是未暴露的盲测集。正式最终验证需要新的未参与调参问题/文档。`source_spans` 表示选定支持证据，`reference_context_spans` 可额外展示完整表格；严格覆盖用前者取并集计算，并保留语义评测，不能把替代证据召回一律当作答案错误。

已核对 14 份规范文档的哈希；全部证据和参考上下文区间都能逐字符还原，9 条表格题都有表头。机器校验不代表语义审核通过。人工修订应另存新版本并记录审核人、日期及版本哈希。重建命令：`/opt/homebrew/opt/python@3.11/bin/python3.11 pilot/rag_pipeline/review_eval_dataset.py`。

### M2-new：canonical v1 的 Ragas 候选批（v3 已生成，正式冻结待做）

这是一条独立于旧 44 条 QA/M4 的新主线，不能把候选直接并入旧 QA：

- Ragas 0.4.3 `TestsetGenerator` 从中文 canonical v1 的全部 29 份文档生成 30 条 single-hop 候选；出题/参考答案模型为 `deepseek-v4.1-flash`，摘要 embedding 为 `text-embedding-v3`。
- 生成脚本使用确定性逐文档覆盖，确保 29 份文档都至少有一条候选；因此有 30 条候选、29 个来源文档，其中个人信息保护法有两条。该适配只规避 Ragas 0.4.3 persona 名称映射异常，问题/答案仍由官方 single-hop synthesizer 生成。
- 已脚本化检查 30/30 条的 canonical SHA、完整 `reference_contexts`、来源路径、mapping sidecar 和条款级 evidence blocks；6 条候选是多条款证据题，2 条因问题范围宽于当前答案被暂缓收窄。
- 代理审核结果仍标为 `agent_reviewed_candidate_pending_human_approval`。条款级区间是保守的完整文章块，不等于已经完成人工语义审核或法律效力认证。

阅读入口：

- [v3 候选原始输出](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates.json)
- [v3 代理审核候选](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_agent_reviewed.json)
- [v3 项目 QA 形状](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_project_shape.jsonl) 和 [候选 manifest](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_project_manifest.json)
- [v3 回源审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/review_packet.md)
- [v3 回源脚本](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/review_ragas_candidate_batch_v3.py)

**本阶段下一步：**逐题确认问题是否自然、参考答案是否完整、条款条件/例外是否遗漏、日期/跨法规引用是否需要收紧；然后补充多跳、不可回答、近似条款和真实业务问法，生成正式 QA manifest 与调参/保留集。正式 QA 冻结前不启动新的 B/C 模型网格。

### M3：最小闭环的实际结果

已在独立锁定依赖的环境中完成“分块 → 向量化 → 检索 → 回答 → 评分 → 保存”。预选 q0007、q0012、q0032、q0034，2 英文/2 中文，来自 3 份文档；全体 14 份文档参与检索。B/C 统一为 512 个本地 tokens、重叠 51；分别生成 34/88 个块，最终最大块长 502/485，正文映射无 unresolved。

两个上下文视角共 16 份回答、64 项正式指标评分；v2 全部成功。中英文各一组正/反控制，共 12 项控制评分，正例均 1、反例均 0。控制只检查明显方向，不能证明 judge 已通过业务校准。

阅读入口：

- [运行配置 v2](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_config_v2.json)：数据、模型、计量、检索、提示词参数、指标与重试规则。
- [运行说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M3_RUNBOOK.md)：安装、复现、恢复、依赖兼容问题及 v1/v2 区别。
- [结果报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/report.md)：同题回答、指标和完整上下文链接。
- [代理逐题检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/agent_review.md)：答案遗漏、步骤关系、评分拆解问题及后续处理；不是人工通过记录。

v1 暴露参考答案指代问题：Factual Correctness 不读取问题，`It` 开头的答案使正确命名被判错。v2 在配置中为四题补全主语/领域，原参考答案、QA 父集和证据保留，对 B/C 统一重评；不混合两版分数。还发现步骤关系未被 Faithfulness 捕获、事实拆解添加额外含义，以及 Context Precision 接近 1 仍可含无关块。详情见代理检查。v1/v2 标准价用量估算合计约 0.633129 元，不是实际账单。

### M4 执行：参考、judge、候选和完整网格已形成具体产物

**当前入口：**[M4_RUNBOOK](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_RUNBOOK.md)。[初始 v4 报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_text_initial_v4/report.md) 已完成，自动预算通过 B 24/24、C 23/24；[初始回源检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_text_initial_v4/agent_review.md) 记录可核实漏判，不称人工正确率。网格 `m4_grid_manifest_v4.json` 已完成 16 组独立结果；[网格分析](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_grid_analysis_v4/report.md) 与[代理回源复核](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_grid_analysis_v4/agent_review.md) 已生成。

**2026-10-10 执行更新：**[执行补充 v2](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_EXECUTION_AMENDMENT_v2.md)、[当前状态](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_execution_state_v1.json)。运行使用 `m4_text_initial_config_v2.json` 和 `m4_grid_manifest_v2.json`；v1 未完成记录及协议快照保留。分析入口新增 `analyze_chunking_grid.py`，不会为未完成配置生成完整成功率。

**后续评分修订：**初始比较发现引用已含完整条件却被判遗漏。另存 26 例控制，criteria_v1 为 25/26、criteria_v2 为 26/26 与代理预期一致；不等于独立人工验证。当前实际入口更新为 `m4_text_initial_config_v3.json`、`m4_grid_manifest_v3.json`，所有候选统一用 criteria_v2，详见 [评分补充](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_CRITERION_AMENDMENT_v3.md)。v1/v2 未完成数据、原始判断和缓存保留，QA/答案/四项官方诊断不变。

- [24 题逐题核对材料](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_tuning_review_packet_v3.md)：旧/新参考、64 条必答事实、顺序/条件、主要/可选证据及范围。q0010/q0026/q0032 将问题未要求的细节转为可选；问题和 split 不改，父 QA 保留。
- [调参数据 v3](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_text_tuning_v3.jsonl)、[对应 manifest](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_text_tuning_v3_manifest.json)：24 题、19 英文/5 中文、10 份来源文档；原文/证据和哈希检查通过，人工状态 pending。
- [Judge 控制报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/judge_calibration_v1/report.md)、[代理检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/judge_calibration_v1/agent_review.md)：21 个控制、42 次按题判断和 32 项官方公式诊断；无失败，标准价估算 0.150808 元。两套按题提示词均 21/21 一致，不宣称改善了一致率。严格事实拆解仍让错误步骤关系的 Faithfulness=1，未修复。
- [M4 协议](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_PROTOCOL.md)：保留官方四项指标作诊断，补充项目 AnswerAdequacy（Ragas DiscreteMetric）按必答事实/关系评分，作为探索性主质量指标；不是内置标准指标，也不冒充人工业务认证。
- [候选检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_candidates_v1.md)：18 配置/16 独立块集合，长度与正文映射通过，当前未调用候选模型。
- [初始比较配置](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_text_initial_config_v1.json)、[网格清单](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_grid_manifest_v1.json)：完整配置已保存，初始 prepare-only 通过；回答提示词改为通用 grounded_answer_v2，保留条件/顺序，对所有候选一致且不含 gold 事实。

**旧网格的遗留审核：**人工裁决重复出现的关系错误、拒答漏判和来源归因问题，原评分保留。本轮用户已决定先扩充语料；当前下一步是规范文本/版本审核与新QA，而不是只验证B/512。新实验的主指标及比较规则另行登记，Ragas内置指标和来源证据检查分别报告。人工审核未完成前不宣布最终赢家。

### 后续里程碑

| 里程碑 | 内容 |
|---|---|
| M3 | 最小模型评测已运行；代理检查已保存，人工 QA/judge 验收待完成 |
| M4 | 初始比较 v4 和 16 组不同分块结果已完成；原始自动排序、coverage sanity、失败项和代理回源复核已保存 |
| M5 | 必要消融、保留集验证、最终配置与失败分析 |

**下一批交付物：基于 `zh_enterprise_compliance_canonical_v1` 建立候选问题、参考答案和 canonical evidence spans，然后逐题做官方来源/版本/适用对象审核并冻结 QA。** 之后再进行公平的 B/C 及参数比较。旧网格关键题人工裁决仍是遗留项；不能根据 M3 四题均分、构造控制或旧网格自动均分直接宣布最终配置。

## 7. 环境与实现入口

### 7.1 本机环境

- Python 3.11 已安装，Homebrew 路径为 `/opt/homebrew/opt/python@3.11/bin/python3.11`。
- 项目是 `uv` workspace，`.python-version` 指定 3.11。
- `uv` 可用；无需安装 Python 3.11。
- 系统 `python3` 指向 3.9.6 不代表 3.11 不存在。
- pilot 历史环境：`/Users/zsm/Documents/DB-GPT/pilot/.venv`。
- M3 使用独立环境 `/Users/zsm/Documents/DB-GPT/pilot/.venv-rag-eval`，解释器为现有 Python 3.11；直接依赖和全部解析版本见 `requirements-rag-eval.in/.lock`。
- 评测环境包含 Ragas 0.4.3、langchain-text-splitters 1.1.2、tiktoken 0.9.0。Ragas 仍导入旧 VertexAI 模块，因此将 langchain-community 固定为 0.4.1；0.4.2 会导致导入失败。历史 pilot 环境仍有 MarkItDown 0.1.7；DeepEval/Optuna 未引入。

涉及模型时通过 `DASHSCOPE_API_KEY` 等环境变量提供凭据。本文已移除旧文档的明文 key；不要将实际值写到文档、配置或运行报告。

历史探索曾使用 DashScope `text-embedding-v4`，这只是已有探索线索，不等于正式评测已经固定该模型。输入上限、batch、维度、价格等在接入时按当前官方文档和选定设置确认；旧 API 试跑不是 B/C 效果实验。

### 7.2 DB-GPT 原生链路定位

以下文件可用于后续接入或回看历史排查：

| 功能 | 源码 |
|---|---|
| Word 知识解析 | [docx.py](/Users/zsm/Documents/DB-GPT/packages/dbgpt-ext/src/dbgpt_ext/rag/knowledge/docx.py) |
| PDF 知识解析 | [pdf.py](/Users/zsm/Documents/DB-GPT/packages/dbgpt-ext/src/dbgpt_ext/rag/knowledge/pdf.py) |
| 原生分块实现 | [text_splitter.py](/Users/zsm/Documents/DB-GPT/packages/dbgpt-core/src/dbgpt/rag/text_splitter/text_splitter.py) |
| Git 仓库同步分块选择 | [git_repo_sync_service.py](/Users/zsm/Documents/DB-GPT/packages/dbgpt-serve/src/dbgpt_serve/rag/service/git_repo_sync_service.py) |
| Tongyi embedding 适配 | [tongyi.py](/Users/zsm/Documents/DB-GPT/packages/dbgpt-ext/src/dbgpt_ext/rag/embeddings/tongyi.py) |

历史排查指出 Word 原生解析、PDF 标题/表格处理和 embedding batch 设置可能影响接入。正式改造时应重新核对当前源码及模型接口；某个服务使用结构分块并不能证明它对所有文档都更优。

## 8. 已有文件和拟议文件的边界

```text
pilot/
├── HANDOFF.md                           当前交接入口
├── RAG_CHUNKING_EVALUATION_PLAN.md       详细调研和实施方案
├── .venv/                              历史 pilot 环境
├── .venv-rag-eval/                      独立模型评测环境
├── requirements-rag-eval.in             直接评测依赖
├── requirements-rag-eval.lock           全部固定依赖
├── experiments/chunking/               M3 配置、说明、缓存与版本化运行结果
├── data/rag_corpus/
│   ├── project_docs/                   历史 195 份清洗快照和报告
│   ├── institution_docs/               16 份 Word 源文件与清单
│   └── annual_reports/                 21 份 PDF 源文件与清单
├── parsed/
│   ├── word/                           自定义 Word 解析产物
│   ├── markitdown/                     Word MarkItDown 示例
│   └── markitdown_pdf/                 PDF 示例、批量产物和审查
└── rag_pipeline/
    ├── corpus_baseline.py
    ├── corpus_clean.py
    ├── chunkers.py
    ├── word_parser.py
    └── run_model_eval.py                M3 embedding/回答/Ragas runner
```

`pilot/data/eval/` 已有 QA 历史/v2、正文父子集、24 题 tuning v3 及控制数据。`prepare_tuning_review.py` 按显式决定重建 v3；`prepare_judge_calibration.py` 固定控制，`run_judge_calibration.py` 已实际运行；`calibrated_metrics.py` 提供项目按题评分及诊断提示词变体。`prepare_chunking_candidates.py`/`prepare_grid_configs.py` 生成候选与配置；`run_model_eval.py` 支持四项官方诊断+项目按题评分，`run_chunking_grid.py` 是顺序、可恢复的网格入口。旧 TF-IDF 保留，M4 初始比较/网格已运行，分析与 agent review 已保存。M3 原 runner 的内容哈希快照保存在 `experiments/chunking/source_snapshots/`。

## 9. 历史块形态记录：仅供追溯

原 195 份含代码语料，800/100 字符、代码保护开启：

| 策略 | 块数 | 均长 | 中位长度 | 小于 100 字符 | 围栏不平衡率 |
|---|---:|---:|---:|---:|---:|
| A | 678 | 1,337 | 800 | 0 | 0.6% |
| B | 737 | 1,231 | 778 | 9 | 0.5% |
| C | 1,555 | 560 | 299 | 118 | 0.3% |

历史 B 代码保护探索：关闭约 42.2% 围栏不平衡，开启约 0.5%。代码保护会还原大段代码，可能产生超过名义上限的大块。

这些数字来自旧交接记录，本次没有重新运行；部分探索脚本在临时目录，没有完整固化成可复现实验。**它们是块形态与围栏统计，不是检索或回答评测结果。** A 和代码保护研究当前均不属于第一阶段主实验。

## 10. 本次更新与尚未确定事项

### 本次完成

- 新增详细调研/实施文档及来源索引。
- 完成 28 份无围栏代码块 Markdown 的首轮文本层审核，暂定冻结 14 份作为 QA 输入。
- 新增语料 manifest 和审核报告，记录哈希、来源、表格/图片标记及排除理由。
- 更新交接入口与进度。
- 修正无代码 Markdown 数量、表格子集、语言解释、Word 充分性与 PDF 解析状态。
- 记录用户退出 A、暂不考虑代码的决策。
- 替换旧 60 字符命中方案，明确 Ragas 指标与可选区间覆盖的区别。
- 将未验证参数、拟议文件和后续实现明确标为待确定/待实现。
- 依据冻结语料生成 44 条 QA 代理初审版，记录参考答案、证据文本、字符 offset、文档哈希、题型和 split。
- 实现并运行 B/C 的离线 TF-IDF 检索诊断；结果仅用于链路检查，未作为正式效果结论。
- 完成中文29份候选的 canonical v1 冻结：最小清洗、版本/来源记录、原始哈希、canonical 哈希及 raw→canonical 字符区间映射均已保存；当前状态为 pending QA。

此前完成 QA v2 回源复核、来源映射修复、长度计量及 provider 审计；工作校准明确 M3 → M4 顺序和小网格端到端比较方向。2026-10-09 本轮另存正文子集，安装锁定的独立评测依赖，实现模型 runner，完成 M3 v1/v2 实际 API 评测和代理检查。v1 诊断保留，v2 是当前可阅读闭环结果；人工内容审核及独立 judge 校准仍待完成。

随后完成 M4 执行：24 题参考 v3 与 64 条必答事实、两套控制校准、18 个候选身份的本地检查、初始比较和 16 组独立网格配置。项目按题评分通过本批代理控制，但真实回答仍有步骤/归因漏判；完整网格已完成，最终配置仍待人工裁决和独立材料确认。

最新完成新语料规范化冻结：在初筛形成的29份中文候选基础上，生成 [`zh_enterprise_compliance_canonical_v1`](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1)，保存规范 Markdown、metadata sidecar、raw→canonical 区间映射和冻结 manifest。29/29 份原始哈希与固定 commit 匹配，删除12个明确目录导航块，未添加标题或改写条款；canonical 冻结本身没有模型调用或效果评分。随后先完成 Ragas `TestsetGenerator` 3题小批，再用 DeepSeek 扩展为29份文档、30条候选，并生成[条款级回源审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/review_packet.md)和[代理审核 JSON](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_agent_reviewed.json)。76份GitLab、24份Dwarves仍是独立候选，尚未规范化冻结。

### 尚待确定

正式语料目标、中文补充范围、真实业务 QA 分布、judge 独立人工对齐、细化网格及最终业务验收门槛。M4 v1 的粗搜候选、计量器、模型/检索/预算和探索性比较规则已登记；最终参数需要实际实验及独立验证。

## 11. 给接手者的摘要

> 先读新语料筛选报告、候选目录和 [中文 canonical 冻结报告](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/audit_report.md)：29份中文合规候选已完成面向 RAG 的质量审核、清洗和 canonical 冻结，76份GitLab流程候选、24份Dwarves备选仍是独立候选。不要拿新 canonical 文档替换旧 manifest 后沿用旧证据 offset；旧14份语料及24题 tuning v3、M4 的18身份/16组结果均保留为探索，最终策略参数未定。Ragas `TestsetGenerator` 已从29份文档生成30条 v3 候选，并完成哈希、来源及条款级证据区间检查；这些仍是候选，不是正式 QA。下一步是逐题确认问题、参考答案、版本语境和证据范围，补充多跳/不可回答/近似难例，冻结正式 QA，最后做无上下文对照、来源命中/覆盖检查和独立保留材料。正文主线继续暂缓表格、代码、图片专项，不因文档变长就保证基础 RAG 会低分。
