# DB-GPT RAG 分块策略与参数选型：调研结论、评测设计和实施计划

> 最后更新：2026-10-13

## 2026-10-13 当前执行结果与下一步

**最新选择：**调参阶段已选择 **B_recursive、256本地cl100k_base tokens、overlap=0** 为当前冻结语料的检索默认。完整依据见[选择报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/selection_decision_v1/report.md)，包含全部18配置×两视图的证据指标、原始Ragas及资源字段。预算字符P/R/IoU为10.77%/95.87%/10.68%；B/256/25均值仅高0.0216/0.0676/0.0232个百分点，无重叠默认是事后工程取舍，不是统计等价或全面占优结论。已有控制条件已记录；用户随后决定先跳过保留题验证，下一步转向固定B/256/0后的检索top-k选型。保留题尚未读取，不再把逐题漏召回复盘或新增AnswerRelevancy列为前置。下文初版的审核/评分前置要求以该最新澄清和v2当前内容为准。

中文配对网格 `paired_chunking_grid_v3` 已完成：9 个参数运行、18 个身份及 18 套不同块，1,656 份回答与 6,624 项 Ragas 主评分，最终技术失败 0。完整离线汇总、23 事实组配对区间、参数与题组敏感性及原始 judge 审计已保存。入口为 [RUN.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/RUN.md)、[分析报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/analysis_v1/report.md)、[完整指标](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/analysis_v1/metrics.md)与[judge 原始输出审计](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/analysis_v1/judge_audit.md)。

实际知识库索引包含 29 份冻结文档，23 份是调参问题来源；46 个问法合并为 23 个事实组。holdout 问题没有参与当前调参评分和结果分析，但来源文档已进入索引。后续留出属于未调参问题确认；旧实验的 18/16 块等价关系不适用于本轮。

预算视图的原始自动 F1 均值最高是 C/512/0（72.20%），B 代表是 B/256/51（70.57%），差 +1.63 个百分点，事实组配对区间 [−5.96, +8.96]。四个指标分开比较，原文字符覆盖仅作诊断，不作为主排名。

**在使用事实F1比较候选回答前，需要校准已知异常；证据检索比较可以先进行。** 已复现正确来源引用“根据第九条”被复制进每条声明、因 reference 没有条号而全部被 NLI 判 0 的机制。62 条机械异常记录来自同一事实组的重复配置/问法/视图；原始分数保持可追溯。校准要覆盖正确/错误出处、额外原文事实、遗漏条件及参考范围，修订政策采用统一新版本。当前已根据证据检索指标选择B/256/0默认，原始回答指标辅助展示，保留问题确认现按用户决定暂时跳过，接下来为检索top-k选型。

本次分析器为 [analyze_paired_chunking_grid.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/analyze_paired_chunking_grid.py)，不调用模型、不读取留出问题、不修改原始 run 结果。详细方法、执行恢复偏离、输入哈希和后续决策见相应分析目录。

**2026-10-13 普通 IR 指标补充：**应用户询问，已另存 [现有来源标注文档级 P/R](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/source_document_metrics_v1/report.md)，计算 18 个候选、两视图、两问法及事实组宏平均。G 为 gold 文档 ID 集合，R 为实际上下文文档 ID 去重集合；P=交集文档数/返回文档数，Recall=交集文档数/gold 文档数。`retrieved_context_ids` 是块 ID，不能直接与 gold 文档 ID 比较。预算视图 C/512/0 为 56.16%/100%，B/256/51 为 47.86%/100%。这是事后来源诊断，未变更原四项 Ragas 主指标或重算回答。单来源题的 100% 仅说明找到了指定文档，未保证找到具体支持条款；未穷尽替代来源也影响 Precision 的语义解释。证据级普通 P/R 需要固定证据单元、完整相关性标注和部分覆盖规则，现有 `source_spans` 可作起点，字符覆盖辅助量仍不改名为普通 Recall。重现脚本为 [compute_source_document_metrics.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/compute_source_document_metrics.py)，原始记录、汇总和输入哈希均单独保存。

**块级普通 P/R 初步计算（2026-10-13）：**已把 29 个独立 gold 原文证据区间映射到每个候选的全量块和实际检索块，并计算任何字符相交块的 P/R，同时给出证据字符覆盖 P/R、完整证据区间覆盖率。18 候选×两视图，共 1,656 条逐题数据。预算视图的 B/256/51 块 P/R 17.19%/95.43%，C/512/0 为 19.12%/86.96%；对应证据字符 P/R 为 10.56%/94.89% 和 9.13%/88.22%。这是明确、可复现的 span-offset relevance proxy；交一个字符也会把块计为相关。当前 28/29 span 是整条条文，故结果不能声称是已人工确认的语义相关性。C/512/0 的一题找到来源文档但没有覆盖任何标注证据。整条条文标注不自动说明错误；本轮固定现有标注作比较，结论限定于该口径，逐题漏召回复盘为可选解释。详见 [逐块映射与指标报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/evidence_chunk_mapping_v1/report.md)、[执行脚本](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/map_evidence_to_chunks.py)。

### 2026-10-13 检索优先修订：当前执行顺序

核对 `chunking_evaluation` 的核心源码后，后续设计更新为[检索优先选型协议 v2](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/RETRIEVAL_SELECTION_PROTOCOL_v2.md)。当前直接以既有字符P/R/IoU比较18配置，原始回答和资源指标辅助；逐题漏召回复盘为可选解释，不再要求先完成。事实F1校准在使用其比较回答前进行；AnswerRelevancy为后续可选补充，原828条全量补算尚未实施。两者不再是检索选型的前置。

已完成[证据字符IoU与18配置统一比较](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/retrieval_selection_v1/report.md)：18配置×两视图，共1,656逐题记录、36组汇总，既有P/R全部复核一致。IoU采用去重gold覆盖除以“召回原文总字符（含重复）+未覆盖gold字符”。预算视图B/256/25为P=10.79%、R=95.94%、IoU=10.70%；B/256/0为10.77%/95.87%/10.68%；C/256/51为10.82%/92.76%/10.67%。排序仅用于阅读，差异很小；gold仍有28/29整条article，当前固定标注比较并记录边界，未按排名改gold。补算无模型调用、不读取保留题、不改写原始结果。

主视图继续为2560-token预算，top-5单独报告；调参集选定一个主配置并固定规则后，保留题用于确认该选择。默认B/256/0及控制条件已记录，7组保留问题的初步确认已按用户决定暂时跳过，当前下一项工作改为检索top-k选型。

### 2026-10-13 初版流程记录（执行顺序已由v2更新）

本项目后续实验设计已另存为[选型、固定配置与保留题验证协议](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/paired_chunking_grid_v3/SELECTION_AND_VALIDATION_PROTOCOL_2026-10-13.md)。它是既有结果之后登记的流程，不冒称原网格运行前预注册或Ragas必选步骤。

1. 完成已发现的事实F1引用惩罚等评分校准，并核对问题、参考答案和证据范围；任何修订采用统一新版本，保留原结果。
2. 以固定2560本地token预算为主要视图，统一比较18个完整分块配置。top-5单独作为补充视图。回答正确/完整、有原文支持是主要质量判断；证据字符P/R、具体失败和资源量辅助解释，FactualCorrectness单项原始均值不直接决定胜者，各指标不简单平均。
3. 作为辅助指标，计划对18配置预算视图的828条既有回答补算AnswerRelevancy。当前已有 `user_input`、`response` 和模型/embedding配置；新增评分调用即可，尚未实施。原四项Ragas加块/字符四指标共8项，补算后9项；不改称原网格已有该分数。
4. 23个调参事实组/46个问法用于选定一个主配置并记录理由。调参阶段可以整理少量候选，最终进入保留题前固定一个主配置、数据/评分版本、模型、检索、提示词及验证判读规则。
5. 再使用7个保留事实组/14个问法确认已选定配置的表现。保留题不承担重新搜索18配置或按保留题最高分选择赢家的任务。其来源文档已在索引，样本也很小，只提供未调参问题的初步确认。

如果依据保留题结果继续改参数、提示词或选择方案，应记录这批题已经参与开发，新的独立确认需使用未参与调整的材料。分块选型阶段继续沿用已有检索设置，缩小top-k的实验放在后续独立阶段。当前下一项工作仍是评分校准，尚未完成最终选型、AnswerRelevancy补算或保留题验证。

## 运行前与历史记录（下方早期“当前/下一步”以本节为准）

> 文档性质：当前项目事实、已有调研依据、已确认决策和后续实施设计的汇总。  
> 当前进度：全 24 题初始比较 v4 和 18 个声明参数身份/16 种不同分块结果的网格 v4 均已完成；每组 48 条回答，最终技术失败 0。QA v3、criteria_v2、并发 4 与统一截断恢复已登记；实际 judge 仍有关系/归因漏判和拒答漏判，coverage sanity 与代理回源复核已保存，人工审核 pending。粗搜结果已生成，最终策略/参数未选定。  
> 本文中的数据结构、目录和流程，凡注明“拟议”“建议”“待实现”的，均不代表已经落地。

**最新工作方向与产物（2026-10-12）：**用户决定先扩充真实Markdown长文，旧14份/24题结果保留为探索。新语料初筛已完成：[筛选报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/corpus_screening_2026-10-10/report.md)、[逐文档目录](/Users/zsm/Documents/DB-GPT/pilot/experiments/corpus_screening_2026-10-10/candidate_catalog.md)。29份中文企业合规候选286,552本地tokens，76份英文GitLab政策/流程候选286,410；另有24份Dwarves备选。中文29份已生成 [canonical v1 冻结语料](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1)，含最小清洗、哈希、metadata sidecar 和 raw→canonical 区间映射。基于 canonical v1 已用 Ragas 0.4.3 `TestsetGenerator` 和 `deepseek-v4.1-flash` 从29份文档生成30条 v3 候选，并完成脚本化来源、哈希和条款级证据区间检查；候选仍未冻结为正式 QA。**当前先逐题审核/修订 v3 候选并补充题型，再登记新协议并比较B/C；本文件下方历史“直接确认B/512”的下一步不再是当前工作顺序。**全文法律效力、模板/外链依赖及内容许可记录仍需按报告边界处理，不用长文或高分直接证明策略优劣。

**2026-10-10 执行更新：**初始 v1 已生成 96 份回答，评分尚未完成，现恢复为另存的执行 v2，统一请求并发上限 4 并增加缓存写入保护。实验候选、QA、模型、提示词及比较规则不变。当前入口为 `m4_text_initial_config_v2.json`、`m4_grid_manifest_v2.json`；详见 [执行补充](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_EXECUTION_AMENDMENT_v2.md)。以下 v1 准备状态保留历史含义，完成情况以执行状态和实际结果为准。

**后续当前版本：**引用/额外信息规则另存 criteria_v2，扩展 26 控制中旧规则 25/26、新规则 26/26 与代理预期一致；真实回答仍漏关系/归因。初始 v3 的一项输出截断以统一扩大上限恢复，当前初始/网格入口为 v4。详见 [M4_RUNBOOK](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_RUNBOOK.md) 和 [初始回源检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_text_initial_v4/agent_review.md)。旧状态和配置保留为历史，不代替实际完成状态。

> 离线诊断更正：早期 TF-IDF 百分比受到来源映射错误、QA 语言失配及覆盖区间重复计数的影响，已撤回其策略排序解释。后续 QA v2 和字符回归已修正这些问题；即使修复后，TF-IDF 仍仅作链路诊断，不能代替正式语义检索和回答评测。

### 阅读导航

- [目标与当前决策](#1-要解决的问题与当前结论)
- [当前产物与语料情况](#3-当前项目已经具备什么)
- [B/C 实现及来源映射限制](#6-现有-bc-实现以及影响实验的限制)
- [公开方案与复用能力](#7-公开方案与可复用能力)
- [评测集建立、字段与数据划分](#8-评测集怎么建立)
- [指标与证据覆盖的依据](#11-到底评测什么检索层与回答层)
- [公平比较与参数选型](#13-如何保证不同分块方案比较公平)
- [批量实验与结果记录](#16-批量实验怎么实现和记录)
- [下一步里程碑](#19-下一步实施里程碑与验收产物)
- [历史记录修正与来源索引](#21-历史记录的保留与纠正)
- [候选语料清单](#附录-a28-份无围栏代码块-markdown-候选清单)

## 1. 要解决的问题与当前结论

### 1.1 实验目标

针对我们的知识库和实际查询，确定：

1. 采用什么分块策略。
2. 使用什么块大小和重叠参数。
3. 是否需要标题注入、标题层级调整、表格保护等处理。
4. 这些选择如何影响检索、最终回答、上下文长度、延迟和成本。

最终需要得到一个可追溯的结论：在明确的语料版本、查询分布、模型和检索配置下，某个方案比其他候选更适合本项目。不能仅凭块数、平均长度、几个示例或框架默认值宣布某方案更优。

### 1.2 当前推荐路径

采用现有 **B：递归字符分块**、**C：Markdown 结构分块 + 递归二次切分 + 标题注入**，配合 **Ragas** 的评测数据结构和指标，补充本项目的批量实验编排、来源元数据和结果记录。

推荐顺序：

```text
明确第一阶段语料边界
  → 冻结可追溯的语料版本
  → 建立并审核固定 QA 集
  → 划分调参集和保留评测集
  → 接入批量分块、索引、检索和评测
  → 完成 B/C 初始对比
  → 根据结果联合调整策略与参数
  → 用保留集确认最终方案
  → 记录错误类型、成本和适用边界
```

这里复用的是已有框架能力和公开实验流程；我们仍需实现与当前代码、语料和模型接口之间的连接。不存在一个无需提供自己的知识库和 QA、无需适配现有链路，就能自动给出“本项目最佳分块方案”的通用开源工具。

### 1.3 这是不是通过实验来确定

是。公开文档和案例支持使用固定评测问题、对不同配置构建索引并比较结果的做法。例如，LlamaIndex 的参数优化示例把 `chunk_size` 和 `top_k` 作为候选参数，由目标函数构建索引、查询并评分；AutoRAG 的传统 Python 流程提供候选配置评测及不同分块语料的标注映射。它们证明这一实施路径已有公开实现，但不能替我们证明某个参数在本项目中有效。[LlamaIndex 参数优化示例](https://developers.llamaindex.ai/python/examples/param_optimizer/param_optimizer/)、[AutoRAG 数据创建教程](https://marker-inc-korea.github.io/AutoRAG/data_creation/tutorial.html)

## 2. 已确认、推荐与待确定事项

| 项目 | 状态 | 具体含义 |
|---|---|---|
| 删除 A 固定长度作为候选实验组 | 用户已确认 | 后续主实验只比较 B/C；当前代码仍保留 A 函数和注册项 |
| 第一阶段研究正文分块，暂缓代码块及表格专项 | 用户已确认 | 不增加表格处理，不以表格题评测/调参；源文档中的表格文本留在现有语料版本内 |
| 通过评测选择策略及参数 | 已确认方向 | 需要固定 QA 和批量实验，不凭默认值选定 |
| 先调研公开方案与业界做法 | 已完成一轮调研 | 来源及可复用能力见第 7 节和附录 |
| 现有 B/C + Ragas + 项目实验编排 | M3 最小链路已实际运行 | Ragas 0.4.3、模型 runner、16 份回答/64 项评分；正式网格及人工校准仍待完成 |
| 14 份 Markdown 作为首轮 QA 输入 | 已完成暂定冻结 | 来自 28 份无围栏代码块候选；详见 `pilot/data/eval/corpus_manifest.json` |
| 中文 canonical v1 语料 | 已完成冻结，QA 候选已生成 | 29 份中文法规 Markdown；见 `pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/`。结构冻结不等于当前法律效力逐份认证 |
| QA 代理初审版 | 已完成暂定版 | 44 条，覆盖 14 份文档；见 `pilot/data/eval/qa_provisional.jsonl` |
| QA 回源复核 v2 | 已完成代理复核 | 44 条；31 英文、13 中文；9 条表格题；见 `qa_review_packet_v2.md` |
| 首轮正文子集 | 已另存，按用户范围决定 | 35 题：27 英文/8 中文，调参 24/原保留 11；见 `qa_text_only_v2.jsonl` 与对应 manifest |
| 正文调参参考 v3 | 已完成代理核对 | 24 题、19 英文/5 中文、64 条必答事实；问题/ID/split/原文不改，旧参考/父集保留 |
| Judge 控制诊断 | 已实际运行 | 21 例、两套按题提示词各 21/21 与代理控制一致；官方诊断仍漏关系，不能称人类专家对齐 |
| QA 人工内容审核 | 待做 | 当前正文调参阅读材料为 `qa_tuning_review_packet_v3.md`；完整 v2 材料保留，代理判断不等于人工通过 |
| 调参/保留集初始划分 | 已完成暂定版 | 按完整文档划分，30 条调参、14 条保留；见 `qa_splits.json` |
| 正式分块参数单位 | 用户已接受 token | 需登记固定的本地 tokenizer；若不同于 provider，明确计量差异并另核对输入限制；最终标题注入也占块长度 |
| 历史模型设置 | M3/M4 已运行并冻结在历史结果中 | embedding 为 `text-embedding-v3`、1024 维，回答/judge 为 `qwen-plus-2025-12-01`；21 例代理控制已运行，独立人工校准待完成 |
| 后续新轮次模型设置 | 已切换 | 当前 profile 的 LLM 为 `deepseek-v4.1-flash`；后续 QA 生成、回答和 judge 统一使用它，embedding 继续为 `text-embedding-v3`。新旧缓存和结果不混用，详见 `pilot/experiments/MODEL_POLICY_2026-10-12.md` |
| 离线检索诊断 | 已完成初始点 | 现有 B/C + TF-IDF，800/100，top-k 1/3/5；不等于正式 Ragas 结果 |
| 中文企业知识库是否为正式目标 | 仍需明确 | 当前候选 Markdown 多为英文技术文档，不能直接代表中文企业制度/SOP |
| `chunk_size`、overlap 最终取值 | 未确定；粗搜候选已准备 | 256/512/1024 本地 tokens、0/10/20% overlap；18 配置/16 独立块集合，均通过本地检查，未跑模型网格 |
| 出题、回答、评审模型 | 新候选/后续新轮次统一使用 DeepSeek | `deepseek-v4.1-flash` 负责候选生成、回答和 judge；`text-embedding-v3` 负责 embedding。旧 Qwen 输出只作历史结果，正式 judge 的可靠性及自评偏差仍需核对 |
| 评测集最终题量、类型比例、验收阈值 | 未确定 | 不存在适用于本项目的统一数字标准 |
| 表格处理的具体规则 | 用户已暂缓 | 当前 B/C 仅按普通 Markdown 文本切分；首轮不实现表格保护或表格评测 |
| 精确原文区间覆盖指标 | 辅助诊断 | 最新字符回归的正文映射已修复；其他参数、注入标题及裁剪后上下文仍需检查，不能自动沿用其可靠性 |
| 多模态图片召回及复杂 PDF 表格 | 后续专项 | 第一轮分块策略实验不以此为目标 |

**记录原则：**“推荐”不能写成“已实现”，“公开示例参数”不能写成“行业标准”，“本地块形态统计”不能写成“RAG 效果评测结果”。

## 3. 当前项目已经具备什么

### 3.1 已有文件

| 产物 | 位置 | 当前用途 |
|---|---|---|
| 历史 Markdown 筛选清单 | [_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs/_manifest.json) | 追溯 195 份保留文件及原始分类 |
| 筛选报告 | [_report.md](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs/_report.md) | 了解历史筛选口径 |
| 首轮语料 manifest | [corpus_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/corpus_manifest.json) | 暂定冻结 14 份 QA 输入及全部 28 份审核记录 |
| 首轮语料审核报告 | [_corpus_review.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/_corpus_review.md) | 纳入/排除理由、哈希和审核边界 |
| QA seed | [qa_seed.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_seed.jsonl) | 依据冻结原文行范围编写的 44 条问题候选 |
| QA 代理初审版 | [qa_provisional.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_provisional.jsonl) | Ragas 对应字段、答案证据和字符 offset；待人工确认 |
| QA 审核日志 | [qa_review_log.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_review_log.md) | 逐题人工审核占位和审核字段 |
| QA 质量报告 | [qa_quality_report.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_quality_report.md) | 覆盖、题型、划分和限制 |
| QA 划分 | [qa_splits.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_splits.json) | 按文档隔离调参/保留集 |
| QA 准备脚本 | [prepare_eval_dataset.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/prepare_eval_dataset.py) | 解析行范围、校验哈希、生成 QA 元数据 |
| QA 逐题阅读材料 v2 | [qa_review_packet_v2.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_review_packet_v2.md) | 每题问题、答案、原文、来源、修改理由、旧题目 |
| QA 修订数据 v2 | [qa_source_reviewed_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_source_reviewed_v2.jsonl) | 57 个支持区间、52 个参考上下文区间，来源 ID/版本与审核状态 |
| QA 复核日志 v2 | [qa_audit_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_audit_v2.jsonl) | 44 条实际代理判断及修改记录，人工状态 pending |
| QA 质量报告 v2 | [qa_quality_report_v2.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_quality_report_v2.md) | 复核结果、校验、冲突、数据独立性及适用边界 |
| QA 版本 manifest v2 | [qa_version_manifest_v2.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_version_manifest_v2.json) | 输入/输出及生成脚本哈希，划分与诊断暴露记录 |
| QA 复核重建脚本 | [review_eval_dataset.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/review_eval_dataset.py) | 从原 seed + 显式逐题决策重建 v2，检查坐标和表头，不自动进行语义审核 |
| 新 Ragas v3 候选原始批 | [qa_candidates.json](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates.json) | 29 份 canonical 文档、30 条 DeepSeek single-hop 候选；正式 QA 仍待审核 |
| 新 Ragas v3 回源审核 | [review_packet.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/review_packet.md)、[qa_candidates_agent_reviewed.json](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_agent_reviewed.json) | SHA、完整来源和条款级 evidence blocks 已检查；仍不是人工批准 |
| 新 Ragas v3 审核脚本 | [review_ragas_candidate_batch_v3.py](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/review_ragas_candidate_batch_v3.py) | 显式条款映射、raw→canonical 回源和候选状态生成 |
| 新 Ragas v3 项目 QA 形状 | [qa_candidates_project_shape.jsonl](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_project_shape.jsonl)、[qa_candidates_project_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_project_manifest.json) | 转成项目统一字段；split 未分配，`formal_qa_ready=false` |
| Ragas 字段适配器 | [ragas_adapter.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/ragas_adapter.py) | 转换为 `user_input`、`reference`、`reference_contexts`；不负责评分 |
| 最小模型评测 runner | [run_model_eval.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/run_model_eval.py) | 真实 embedding、精确余弦检索、回答和 Ragas 0.4.3 四项指标；成功缓存、失败与配置快照 |
| M3 配置与运行说明 | [M3_RUNBOOK.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M3_RUNBOOK.md) | 本轮控制设置、依赖兼容、v1/v2 参考答案修正、复现和恢复 |
| M3 结果与代理检查 | [report.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/report.md)、[agent_review.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/agent_review.md) | 四题同题上下文/回答/评分，原始判断诊断与后续处理；人工状态仍 pending |
| 离线检索诊断 runner | [evaluate_chunking.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/evaluate_chunking.py) | B/C + TF-IDF 的链路诊断，不生成回答 |
| 离线诊断对照 | [diagnostic_comparison.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/diagnostic_comparison.md) | 800/100、top-k 1/3/5 的初始对照和限制 |
| 清洗报告 | [_clean_report.md](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs/_clean_report.md) | 了解清洗规则、统计和语言代理指标 |
| 清洗统计 | [_clean_stats.json](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs/_clean_stats.json) | 逐文档检查清洗结果 |
| 清洗脚本 | [corpus_clean.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/corpus_clean.py) | 复用现有规则，后续记录清洗版本 |
| 筛选脚本 | [corpus_baseline.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/corpus_baseline.py) | 理解原始候选快照的生成方式 |
| 分块器 | [chunkers.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/chunkers.py) | B/C 的现有实现，仍包含 A |
| Word 自定义解析器 | [word_parser.py](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/word_parser.py) | 解析标题、正文、表格，后续需审核质量 |
| Word 解析报告 | [_parse_report.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/word/_parse_report.md) | 查看 16 份解析产物的统计 |
| PDF 批量转换摘要 | [batch_summary.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/markitdown_pdf/batch_summary.md) | 查看 21 份 PDF 的 MarkItDown 转换记录 |
| PDF 表格审查 | [batch_table_audit.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/markitdown_pdf/batch_table_audit.md) | 查看批量诊断与代表性问题 |
| 东方财富年报表格专项审查 | [eastmoney_table_audit.md](/Users/zsm/Documents/DB-GPT/pilot/parsed/markitdown_pdf/eastmoney_table_audit.md) | 查看单份年报的表格审查记录 |

### 3.2 已跑通的链路与尚未具备的产物

截至本次记录：

- `pilot/data/eval/` 已建立，包含首轮语料、QA seed、代理初审 QA、划分清单、质量报告和审核日志。
- 已有 `qa_source_reviewed_v2.jsonl`，逐题代理回源复核完成；尚无经过人工内容审核的最终 gold 集。原 `qa_provisional.jsonl` 保留历史状态，后续不能与 v2 混用。
- 已有 B/C 的离线分块和 TF-IDF 诊断，以及新的 `run_model_eval.py`；后者已经跑通真实 embedding、回答、Ragas 评分和报告闭环。
- `ragas_adapter.py` 仍只负责字段转换；真实评分由独立评测环境中的 Ragas 0.4.3 与模型 runner 完成，不能把历史 provisional 输入称为已经全量评分。
- M3 v2 已保存四题 × 两策略 × 两视角的 16 份回答和 64 项指标，另有 12 项正反控制；全部成功，属于试跑结果。
- 全 24 道正文调参题初始比较和参数网格已完成；judge 独立人工校准、关键失败题人工裁决和最终保留集确认仍待完成。
- 尚未确定细化参数范围与最终配置；粗搜候选已经准备。

补充的 M4 准备产物已经完成：`qa_text_tuning_v3.jsonl`/逐题材料、21 例校准控制及实测报告、`M4_PROTOCOL.md`、候选本地检查和可执行配置。粗搜候选已明确，最终细化范围/最优值尚未确定。初始比较仅通过 `--prepare-only`，不能将配置或块统计称为实际 24 题模型效果。

当前 30/14 划分保持文档隔离，但原保留题已用于历史 TF-IDF 诊断，不能再称从未暴露的盲测材料。正式最终确认需新的未参与调参文档/问题。

历史 `pilot/.venv` 中有 langchain-text-splitters 1.1.2、MarkItDown 0.1.7。M3 新环境为 `pilot/.venv-rag-eval`，包含 Ragas 0.4.3、langchain-text-splitters 1.1.2、tiktoken 0.9.0，全部依赖固定于 `pilot/requirements-rag-eval.lock`。为兼容 Ragas 的旧 VertexAI 导入，langchain-community 固定 0.4.1；0.4.2 会导入失败。实际 API 试跑已完成；DeepEval、Optuna 未引入。

## 4. 当前语料的真实情况

### 4.1 195 份 Markdown 是历史保留快照

原始筛选记录：扫描 212 份 `.md`，保留 195 份，剔除 17 份。历史阈值是非空白字符数至少 500 等规则。

清洗后 195 份共 **666,117 个非空白字符**。这里的“字数”是脚本字符统计，不是模型 token 数，也不是自然语言正文的人工计数。

目前源文档目录已经发生变化，因此 212/195 表示已有清单对应的历史快照，不能解释成当前仓库所有 `.md/.mdx` 文件的实时总数。第一轮实验应直接冻结选定快照，避免过程中自动混入新增文件。

### 4.2 围栏代码块和表格分类

按原始 manifest 中的 `code_blocks` 与 `table_lines` 字段分类：

| 类别 | 文件数 | 第一阶段处理 |
|---|---:|---|
| 无围栏代码块、无被规则识别的 Markdown 表格 | 21 | 正文候选 |
| 无围栏代码块、有被规则识别的 Markdown 表格 | 7 | 正文 + 表格候选 |
| 有围栏代码块、无表格 | 112 | 暂不进入主实验 |
| 有围栏代码块、有表格 | 55 | 暂不进入主实验 |
| 合计 | 195 | 历史完整快照 |

因此：

- 含围栏代码块的文件共 **167 份**。
- 含表格的文件共 **62 份**，其中 **55 份同时含代码块**。
- 暂不考虑代码块时，候选池为 **21 + 7 = 28 份**。
- 不能把“21 份无代码正文 + 62 份所有表格文档”合并成 83 份无代码语料。

分类边界需记录：

1. 围栏代码块主要按三反引号规则识别；围栏里的图示等也可能被计入。
2. “无代码块”不等于“没有行内代码”，也不保证不存在缩进代码或其他写法。
3. “无表格”指当前规则未识别出 pipe 表格，不保证没有 HTML 表格或其他列表式结构。
4. “纯文字 Markdown”只是便于讨论的简称；文件仍可能含链接、图片引用、标题、列表。
5. 清洗后重新用另一种简单规则检查表格，数量可能与原 manifest 不同。正式实验应冻结统一定义并人工核验，而不是混用统计口径。

### 4.3 第一轮 28 份候选的大小与质量边界

按当前清洗文件统计：

| 子集 | 文件数 | 非空白字符数 |
|---|---:|---:|
| 无代码、无表格 | 21 | 37,107 |
| 无代码、有表格 | 7 | 10,642 |
| 合计 | 28 | 47,749 |

这批材料足够用于建立评测流程、发现实现问题和进行初步比较，但存在几个明确限制：

- 内容以 DB-GPT 技术说明为主，不能代表所有企业知识库。
- 多数候选以英文为主。使用中文提问会同时考察跨语言检索能力。
- 一些页面是导航页、链接索引或产品简介，可回答的问题有限。
- 表格页面中有不少工具、连接或集成列表，不等于包含丰富的制度表、业务数据表。
- 个别文档清洗后明显变短。例如 `tutorials/index.md` 仅约 215 个非空白字符，需要在冻结时审核。
- 195 份文件的全文哈希未发现完全相同文件，这不能排除近重复内容、同事实不同措辞或版本重复。

语言代理指标采用：

```text
Han 字符数 /（Han 字符数 + ASCII 字母数）
```

这个指标会受 URL、英文术语和代码影响，只适合筛查。28 份候选中，按该口径达到 50% 的仅 2 份：`installation/index.md`、`application/datasources.md`。不能据此直接给每份文件作人工语言质量认证。

### 4.4 中文 Word 可作为补充候选，但需逐份审核

现有 16 份 Word 已有自定义解析产物。15 份含表格，仅《山西农业大学规章制度管理办法》未检出表格。

可优先审查的正文较丰富文件包括：

- 《山西农业大学规章制度管理办法》。
- 《政府采购公开招标文件（货物类）》。
- 《公开招标文件（12.10 最新修改）》。
- 《厦门大学材料学院实验室危险废物处置管理办法》。
- 部分采购、实验室管理等制度文件。

另有申请表、自查表、短通知、制度汇编目录等。表格目录只有文件名称，不等于已有完整制度正文。仅几十个正文字符的表单也不适合承担大量常规问答。

**解析质量需单独审核：**自定义解析器会把某些编号条款和项目提升为 Markdown 标题。例如《山西农业大学规章制度管理办法》有较多被识别的标题；编号条款是否真应形成该层级，需要回看原 Word。机器统计的标题数不能当作结构正确率。

此前“16 份 Word 做不了有统计意义的实验”的表述过强，应撤回。统计能力取决于独立问题数、问题覆盖、重复程度和结果方差，不能仅凭文档份数判断。现有 Word 是否足够，需在内容审查和 QA 设计后决定。

### 4.5 21 份 PDF 已经解析，当前产物不适合直接承担主实验

已有 21 份 PDF 经 MarkItDown 转换，合计 5,506 页，输出约 17.8 MB Markdown。产物在 `pilot/parsed/markitdown_pdf/batch/`。

现有检查表明：

- 普通文本的字符二元组覆盖很高，但该统计不代表阅读顺序、表格语义和结构正确。
- 21 份产物未产生 Markdown 标题层级，也未产生图片 Markdown 引用。
- 复杂财务表格存在错列、断行、多级表头丢失、数字拆开、伪表格等问题。
- 小型、边界明确的简单表格有些可读，不能把所有表格一概判定为不可用。
- 报告中的表格分隔线出现次数不是实际表格数量；PDF 图片对象出现次数也不是有业务意义的独立图片数量。
- 已有批量机器诊断和代表性抽查，不等于每一个表格都完成了人工正确性认证。

如果直接拿这些无 Markdown 标题的产物测试 C，C 的结构能力可能无法发挥。得到的差异会同时受解析损失影响。因此第一轮应优先使用结构可控的 Markdown，或经过审核的 Word 转换文本；PDF 的版面、表格与图片提取另设阶段。

## 5. 第一阶段语料如何冻结

### 5.1 建议的边界

第一轮已从 28 份无围栏代码块 Markdown 候选中暂定纳入 14 份，形成 `md_no_code_2026-10-09_v1`。剩余 14 份保留在审核记录中但暂不进入 QA 输入。若需补充中文制度文件，先审核其解析结果，再作为清楚标识的独立子集加入并生成新版本。

建议把两个目标区分为阶段：

| 阶段 | 用途 | 可以得出的结论 |
|---|---|---|
| 流程试跑 | 验证 QA、检索、评分、日志和比较流程是否连通 | 实现是否可用、明显错误在哪里 |
| 正式选型 | 使用符合目标用户与知识库的语料和问题 | 在明确范围内推荐策略及参数 |

若正式目标仍是中文企业制度/SOP，需补充同领域中文文档和自然业务问题，或明确正式结论只针对本次技术文档子集。

### 5.2 冻结前的检查

逐文档至少记录：

- 是否有足够实质内容可支持问题。
- 清洗是否误删重要标题、图片说明、HTML 表格或正文。
- Markdown 标题层级是否合理。
- 表格是否保留行列关系、表头、单位和脚注。
- 是否属于导航页、空壳、目录、申请表或重复版本。
- 主要语言、领域、文档类型、长度和结构深度。
- 纳入或排除原因。

已清洗只是“运行过规则”，不等于内容已全部审核。本轮已经完成候选文本层筛选，但 QA 仍未审核。需要保留原始文件及清洗后的规范文本；清洗后生成的 offset 应锚定清洗后文本，不能直接当作原 DOCX/PDF 的字符位置。

### 5.3 拟议的语料 manifest 字段

以下是本项目的可追溯设计，**不是 Ragas 强制格式**：

| 字段 | 用途 |
|---|---|
| `corpus_version` | 区分不同冻结批次 |
| `doc_id` | 一个冻结版本内稳定的文档标识 |
| `source_path` | 原始文件来源 |
| `canonical_path` | 实验实际读取的规范文本 |
| `content_sha256` | 发现文件变化，避免旧标注对应新文本 |
| `parser_version` / `cleaner_version` | 追溯解析和清洗规则 |
| `language` / `domain` / `document_type` | 分层统计、解释代表性 |
| `has_table` / `has_fenced_code` | 明确本轮边界 |
| `heading_depth` / `char_count` / `token_count` | 选择参数和诊断块形态 |
| `review_status` / `review_notes` | 记录人工审查状态 |
| `included` / `exclude_reason` | 复现最终输入名单 |

源文件变化后，应生成新语料版本并核对旧 QA。不能只更新同一路径的文件，然后沿用旧 evidence offset。

## 6. 现有 B/C 实现以及影响实验的限制

### 6.1 B：递归字符分块

当前通过 `RecursiveCharacterTextSplitter(chunk_size, chunk_overlap, length_function)` 切分。函数默认仍是 Python 字符长度；M3/M4 实验显式传入已登记的 `tiktoken 0.9.0 / cl100k_base` 本地 token 计量函数。未显式增加中文句号、分号等分隔符，B 的边界设置在本轮候选间保持一致。

其优势是实现直接、对标题结构依赖较少；是否适合本项目需要实测。不能把源码注释中的“业界默认”理解为已经证明其在中文企业文档中最优。

### 6.2 C：结构分块、二次切分与标题注入

当前流程：

```text
Markdown 文本
  → MarkdownHeaderTextSplitter 按已登记标题切分
  → 标题路径保存到 metadata，标题行默认移出正文
  → RecursiveCharacterTextSplitter 二次切分，继承 metadata
  → 将标题路径注入块文本开头
  → 输出 Chunk
```

当前只登记 `#`、`##`、`###`，未登记 `####` 及以下标题。`MarkdownHeaderTextSplitter` 的作用是划分标题结构，本身不是依靠 `chunk_size` 控制长度；二次切分负责限制过大的章节。

标题注入可能帮助独立块表达主题，同时增加向量化文本和模型上下文长度。需要把它作为配置记录，并可在后续消融中验证，不能先假定总是有收益。

### 6.3 尚未实现或不能依赖的行为

| 项目 | 当前情况 | 对实验的影响 |
|---|---|---|
| A 已退出实验 | 仅决策完成，代码仍在 `STRATEGIES` | runner 需显式选择 B/C，不能遍历全部 registry |
| 字符与 token | 默认大小 800、重叠 100，单位为字符 | 不能和外部文档中的 token 参数直接比较 |
| 标题层级 | C 只登记 H1–H3 | H4 条款可能没有对应 metadata |
| 表格保护 | 没有完整专门机制 | 二次切分可能拆表头、表体、单位或脚注 |
| 碎块合并 | 旧讨论提出过规则，当前未实现专门后处理 | 不得把“分类合并碎块”写成已完成能力 |
| 重叠 | B/C 依分隔和章节边界工作 | 实际重叠不一定等于配置值，C 通常不跨独立章节制造重叠 |
| 标题注入后长度 | 已先扣除前缀预算，字符回归合规 | token 计数不保证可相加，正式 runner 需检查最终拼接文本 |
| 来源位置 | `locate_spans` 已改为完整正文空白归一化定位，无命中则 unresolved | 最新字符回归成功；其他参数、重复文本、注入标题和裁剪后上下文需继续审计 |

### 6.4 来源映射：历史问题、已修复部分与剩余边界

历史 `locate_spans` 曾通过片段前 60 或 30 个字符查找，找不到时生成估计区间；这已不是当前实现。历史风险包括：

1. 顺序游标在命中后前移到上一块末尾，可能跳过下一块的重叠起点。
2. 标题剥离、换行重整和空白归一化可能使块文本不再是原文连续子串。
3. 同一句话多次出现时，弱前缀可能定位到错误位置。
4. C 的正文可能由多个原文片段组成，单个连续区间不一定足够。
5. 注入的标题前缀属于变换后的索引文本，不应被算成已经覆盖的原文证据。

当前改为完整正文空白归一化匹配，并保留到原文的字符 offset；按前一块的起点保持顺序，允许重叠；无命中返回 `unresolved`，不生成估计区间。最新字符回归 B 的 74 块和 C 的 116 块正文均定位成功。严格覆盖已先取区间并集，避免重复计数。以上结果限于已运行配置，不保证所有候选都同样成功；注入标题的独立证据区间、重复/合并文本及预算裁剪后来源仍需检查。

**实施要求：**如使用区间覆盖指标，应建立可靠的文本变换与来源映射。无法定位的块记录为 `unresolved`，不伪造区间；报告映射成功率和失配样例。

拟议的块数据模型：

| 字段 | 意义 |
|---|---|
| `chunk_id` | 带配置与语料版本的块标识，不能跨不同分块方案直接当作相同对象 |
| `doc_id` / `corpus_version` | 对应规范文本 |
| `body_text` | 块的正文内容 |
| `embedding_text` | 实际用于向量化的文本，可包含标题注入 |
| `context_text` | 实际给回答模型的文本，若与前两者不同需单独记录 |
| `source_spans` | 一个或多个规范文本区间，不含人为注入前缀 |
| `heading_path` / `element_type` | 结构信息 |
| `char_count` / `token_count` | 实际长度，按选定 tokenizer 计算 |
| `mapping_status` | 来源定位是否可靠 |

这属于拟议适配工作，当前 `Chunk` 类尚未提供全部字段。

## 7. 公开方案与可复用能力

### 7.1 方案比较

| 方案 | 可以复用什么 | 我们仍需提供或实现什么 | 当前判断 |
|---|---|---|---|
| Ragas | QA 生成、评测 schema、检索和回答指标、实验模板 | 语料审核、固定 QA、B/C 索引与检索适配、配置循环、来源映射 | 当前优先方案 |
| DeepEval | RAG 用例结构、检索与回答指标、评测调用及工程集成 | 自己的 pipeline 输出、期望答案、参数实验编排 | 可替代 Ragas，不必一开始同时引入 |
| LlamaIndex 参数优化示例 | 目标函数 + 候选参数 + 构建索引 + 查询评分的流程 | 当前 DB-GPT 链路适配、评测集、指标选择、版本兼容 | 学习流程；不是直接替换现有项目 |
| AutoRAG 传统 Python 方案 | QA/corpus 数据格式、多配置比较、分块后 retrieval ground truth 映射 | 数据转换、自定义分块器接入、环境适配 | 很有参考价值，需注意 legacy 维护状态 |
| Chroma chunking evaluation | 固定问题与证据片段的检索评测、来源区间方法、Recall/Precision/IoU | 当前 B/C 适配与可靠来源映射 | 可用于增强检索诊断 |
| Optuna | 网格或其他超参数搜索、trial 记录 | 从参数到索引、检索、评测分数的目标函数 | 后期可加；小规模循环即可起步 |
| Azure RAG Experiment Accelerator | 配置化实验、云端资源编排与结果记录 | Azure 资源、服务接口、数据与配置适配 | 流程参考；对当前本机 pilot 较重 |
| RagBuilder | RAG 组件与参数优化思路 | 依赖和当前项目适配、维护风险评估 | 仓库声明不再积极维护，不作为优先长期基础 |

来源：[Ragas](https://github.com/vibrantlabsai/ragas)、[DeepEval](https://github.com/confident-ai/deepeval)、[LlamaIndex 参数优化](https://developers.llamaindex.ai/python/examples/param_optimizer/param_optimizer/)、[AutoRAG legacy](https://github.com/Marker-Inc-Korea/AutoRAG/blob/main/legacy/README.md)、[Chroma 评测实现](https://github.com/brandonstarxel/chunking_evaluation)、[Optuna](https://github.com/optuna/optuna)、[Azure 实验加速器](https://github.com/microsoft/rag-experiment-accelerator)、[RagBuilder](https://github.com/KruxAI/ragbuilder)。

这些是相关的官方框架、公开实现及研究案例。它们不能作为“多少企业使用某方案”的市场占有率证据，也没有给出统一最佳块大小。

### 7.2 Ragas 具体负责什么

Ragas 可以接收问题、参考答案、检索上下文和模型回答，执行指标评测。它也提供从文档生成测试问题的能力。

**实际执行边界（2026-10-12）：**旧44条QA由Codex代理依据冻结原文编写，整理/复核后取24条用于M4；未执行`TestsetGenerator`。Ragas已用于实际评分。新一轮先在中文 canonical v1 上完成3题适配小批，随后用官方 `TestsetGenerator` 从29份文档生成30条 v3 候选，保存生成配置、原始输出、DeepSeek模型设置和条款级 canonical evidence spans；候选尚未人工批准或冻结为正式 QA。证据与边界见[来源核对说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/QA_GENERATION_PROVENANCE_2026-10-12.md)、[v3审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/review_packet.md)和[小批审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v1/review_packet.md)，不改写旧QA来源。

`TestsetGenerator` 的文档生成流程会构建内部知识图谱并使用问题合成器；这属于评测集生成机制，不要求我们将实际 RAG 检索改成 GraphRAG。官方也提供提示词语言适配方法，但中文问题是否自然、答案是否准确仍需审核。[Ragas 测试集生成](https://docs.ragas.io/en/latest/getstarted/rag_testset_generation/)、[语言适配](https://docs.ragas.io/en/latest/howtos/customizations/testgenerator/_language_adaptation/)

Ragas 的 `rag_eval` quickstart 提供实验脚手架。模板里的示例评分不等于已经自动接入我们选定的所有 RAG 指标；需要替换示例 pipeline、配置指标、加入 B/C 的索引构建和参数循环。[Ragas RAG 实验模板](https://docs.ragas.io/en/stable/howtos/cli/rag_eval/)

开源 SDK 不等于模型调用免费。QA 生成、参考答案审核辅助、回答和 LLM 评审的成本，取决于所配置的模型和运行方式。

### 7.3 AutoRAG 的重要参考与版本边界

传统 AutoRAG 用固定 QA 配合不同 corpus 版本，更新 QA 的 retrieval ground truth 以适配新的分块语料。这个实现回答了“不同策略块 ID 都变了，如何比较”的一部分问题。[AutoRAG 分块数据流程](https://marker-inc-korea.github.io/AutoRAG/data_creation/chunk/chunk.html)、[QA 映射教程](https://marker-inc-korea.github.io/AutoRAG/data_creation/tutorial.html)

当前仓库的主线已转向 AutoRAG 2.0 Agent；旧 Python AutoML 位于 `legacy/`，文档说明其处于维护模式。不能把旧教程能力无条件视为当前主线同一套接口，也不能认为所有依赖在本机现有环境中已经验证。[AutoRAG 仓库](https://github.com/Marker-Inc-Korea/AutoRAG)、[legacy 说明](https://github.com/Marker-Inc-Korea/AutoRAG/blob/main/legacy/README.md)

### 7.4 为什么当前不直接换成完整新框架

当前已有独立 B/C 分块器，首先需要可审核的语料和 QA。直接引入完整新 RAG 框架会增加数据格式、模型适配和环境成本，并不自动解决语料代表性与标注质量。

项目建议是先用现有组件实现最小实验闭环，复用 Ragas 指标和公开模板；当候选数量或流水线复杂度增加，再评估 Optuna 或其他编排能力。这是当前工程取舍，不是行业强制要求。

## 8. 评测集怎么建立

### 8.1 先固定问题，再比较策略

QA 应独立于某个候选分块结果建立。使用完整规范文档或保留来源关系的文档元素出题，生成并审核一次，然后所有 B/C 配置都用同一套问题。

如果每种分块方案各自重新生成问题，再比较各自成绩，就同时改变了评测对象和题目难度，不能可靠归因于分块方案。

### 8.2 问题来源的优先顺序

建议综合使用：

1. 有来源和使用授权的真实业务问题、知识库查询记录。
2. 依据使用场景由人工编写的问题。
3. 使用 Ragas 或其他生成器产生的候选问题，再人工审核。

当前没有已确认可用的真实用户查询日志。第一阶段可采用人工 + 合成候选，但要在报告中说明数据来源，不能将合成问题包装为真实用户日志。

### 8.3 建立候选 QA 的步骤

1. 从冻结 manifest 读取文档和元数据。
2. 按文档类型、语言、长度、结构和表格情况分层选择出题材料。
3. 让生成器产生问题、参考答案及支持答案的实际文本。
4. 保留生成模型、提示词、时间、文档版本和问题合成器信息。
5. 将候选与源文档关联，核对依据是否真实存在。
6. 审核问题自然度、可回答性、答案正确性和证据充分性。
7. 去掉同事实的重复问法、答案泄露、只能看图片而当前文本不含信息的问题。
8. 审核后冻结 QA 版本，再划分调参集和保留集。

Ragas 导出能提供评测字段及问题合成器信息，但不能假定所有生成器导出都自动带有我们需要的原文件页码、稳定 `doc_id` 和精确 offset。需要检查实际版本输出，补充来源映射。[Ragas schema 源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/dataset_schema.py)、[测试集导出源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/testset/synthesizers/testset_schema.py)

### 8.4 建议覆盖的问题类型

以下是本项目建议的分类，不是框架规定的必选比例：

| 类型 | 关注点 | 示例形式，非现成评测题 |
|---|---|---|
| 单事实定位 | 简单检索是否稳定 | 某功能支持哪些资源？ |
| 条件与流程 | 多句内容是否完整 | 在某条件下需要按什么步骤处理？ |
| 标题依赖 | 章节主题能否帮助理解短正文 | 某章节下的限制或配置是什么？ |
| 长章节细节 | 大块或小块是否漏掉特定要求 | 某说明中对例外情况有什么规定？ |
| 跨相邻段落 | 是否能组合必要证据 | 前提、步骤、注意事项分别是什么？ |
| 表格单元格 | 行、列、字段对应是否正确 | 某工具的适用范围或连接方式是什么？ |
| 表格多行比较 | 多个单元格能否同时召回 | 两个条目在某字段上有什么差异？ |
| 正文 + 表格 | 单位、条件、脚注是否被保留 | 表中值适用于什么条件？ |
| 多处证据 | 需要多个段落或文档 | 对一个主题的不同约束是什么？ |
| 无答案问题 | 是否能正确表示信息不足 | 文档未规定的具体数值或操作是什么？ |

第一轮可以先以单文档问题为主，但不必永久禁止多文档题。多文档证据可以通过多个 `doc_id` 和 evidence span 表示。题型应反映目标使用场景，而不是为了让一种策略得高分而选择。

无答案问题应单独建子集和评分规则。没有支持证据时，不能强行计算普通 evidence coverage，或把正常拒答作为回答错误。

### 8.5 参考答案与证据不是同一个字段

**参考答案**可以是准确的归纳、改写或组合，未必是原文连续片段。

**证据文本**应是文档中实际支持答案的内容，可以分布在多个段落、表格单元格或不同文档。

旧方案要求 `answer in doc_text` 或强制答案为 100–300 字原文，这会排除合理改写、短事实答案和多处证据问题。新的要求是：答案中的关键主张有充分、可追溯的文档支持，而不是答案字符串必须原样出现。

Azure 的准备阶段文档支持记录问题、参考答案、支持答案的实际原文和页码。它并未规定我们的全部扩展字段，也未给出本文第 12 节的区间覆盖公式。[Azure RAG 准备阶段](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-preparation-phase)

### 8.6 人工审核应看什么

正式进入评测集的问题，应完成内容审核：

- 问题是否像用户实际会提出的问题。
- 问题是否在当前知识库范围内，是否存在模糊指代。
- 参考答案是否正确、完整，是否添加文档未支持的信息。
- 证据是否涵盖所有关键主张，能否关联到冻结文档。
- 表格问题是否核对了行、列、单位、日期和脚注。
- 是否依赖未提取的图片、未解析的合并单元格或损坏正文。
- 是否与其他题目重复考察同一事实。
- 问题语言与文档语言是否符合计划中的查询分布。
- 是否需要修订、排除或另设特殊子集。

机器可做格式检查和来源匹配，但不能替代答案内容审核。抽检适合早期了解候选质量；正式集不能仅以“抽检 20 题合格”推定所有题都准确。

### 8.7 题量不是先验固定值

之前出现的 30–50 题试跑、100–150 题、150–300 题等均为探索性规模建议，不是行业统一门槛。

题量应依据：文档可回答信息量、类型覆盖、独立事实数、不同方案的得分差异、人工审核能力和模型预算决定。大量同事实改写不等于大量独立样本。

先做小批候选以校验生成和审核流程，再补足缺失类型；正式实验需报告每类题数、覆盖文档数、重复情况及结果不确定性。不能只报告“总共 N 题”。

## 9. 数据字段如何设计：哪些来自框架，哪些是项目扩展

### 9.1 Ragas 已有字段

`SingleTurnSample` 提供以下相关字段；字段是否必填取决于具体指标：

| 字段 | 用途 | 来自哪里 |
|---|---|---|
| `user_input` | 用户问题 | Ragas |
| `reference` | 参考答案 | Ragas |
| `reference_contexts` | 参考证据文本列表 | Ragas |
| `retrieved_contexts` | 本次运行检索到的上下文列表 | Ragas |
| `response` | 本次回答 | Ragas |
| `reference_context_ids` | 参考上下文 ID，可用于需要 ID 的指标 | Ragas |
| `retrieved_context_ids` | 本次检索上下文 ID | Ragas |

官方定义见 [SingleTurnSample API](https://docs.ragas.io/en/stable/references/evaluation_schema/#singleturnsample)。不要把任意自定义字段直接当成所有 Ragas 指标都会读取的标准列。

### 9.2 需要补充的项目元数据

| 字段 | 为什么需要 | 属性 |
|---|---|---|
| `question_id` | 对齐不同配置对同一问题的输出 | 项目扩展 |
| `dataset_version` / `corpus_version` | 防止题目与文档版本错配 | 项目扩展 |
| `source_doc_ids` | 问题可能依赖一个或多个文档 | 项目扩展 |
| `evidence_spans` | 可选的精确原文区间支持 | 项目扩展 |
| `source_page` / `section_path` | 支持人工回源审核 | 项目扩展 |
| `question_type` / `language` | 分层分析 | 项目扩展 |
| `split` | 区分调参和保留评测 | 项目扩展 |
| `generation_info` / `review_info` | 记录生成方式和审核状态 | 项目扩展 |
| `run_id` / `config_id` | 对齐候选方案和实验记录 | 项目扩展 |

推荐将 Ragas 字段与项目元数据分层存储，再由 adapter 转换为指标需要的样本。Ragas 的 schema 负责评分输入；本项目 manifest 和 sidecar 负责追溯与实验管理。

### 9.3 拟议 QA 记录示例

下面只展示字段层次，**不是真实评测题，不表示对应证据已经审核**。未知 offset 用 `null`，不能伪造坐标。

```json
{
  "question_id": "example-only-q001",
  "dataset_version": "to-be-frozen",
  "corpus_version": "to-be-frozen",
  "sample": {
    "user_input": "示例问题：某项流程需要满足哪些条件？",
    "reference": "示例参考答案：概括已审核的必要条件。",
    "reference_contexts": ["示例证据：应替换为源文档实际文本。"]
  },
  "metadata": {
    "source_doc_ids": ["example-doc-id"],
    "question_type": "procedure",
    "language": "zh",
    "split": "unassigned",
    "evidence_spans": [
      {
        "doc_id": "example-doc-id",
        "content_sha256": "to-be-computed",
        "start": null,
        "end": null,
        "offset_unit": "python-character",
        "text": "示例证据：应替换为源文档实际文本。",
        "mapping_status": "unresolved"
      }
    ],
    "review_info": {
      "status": "not-reviewed",
      "notes": "仅用于说明格式，不进入评测。"
    }
  }
}
```

`retrieved_contexts` 与 `response` 不应提前写成固定 gold 数据。它们是每个配置每次运行的输出，应另存并通过 `question_id` 关联。

### 9.4 不同分块方案的 ID 怎么处理

分块改变后，chunk 的数量、内容和 ID 通常都会变。如果某题 gold 直接写成“必须命中 C 方案的 chunk-7”，拿它评价 B 会不公平。

可采用两类方案：

1. 使用参考答案/证据文本进行上下文支持判断，避免绑定某套 chunk ID。
2. 将 gold 标注锚定于稳定文档、元素或规范文本区间，再映射到每套分块结果。

如使用基于 ID 的 recall，需要明确 ID 对应的是文档、元素还是 chunk，且先建立可验证的映射。文档级命中只能说明找对文件，不等于已找齐答案证据。

## 10. 调参集与保留集如何划分

### 10.1 为什么要划分

在同一套题上反复修改参数再报告最佳分数，容易对这套题过拟合。建议设：

- **调参集**：用于选择策略、块大小、重叠和相关设置。
- **保留评测集**：方案基本确定后用于确认效果，避免持续围绕其结果优化。

这是本项目推荐的实验设计；AutoRAG 文档也强调使用独立的优化和评测数据来降低过拟合风险。[AutoRAG 教程](https://marker-inc-korea.github.io/AutoRAG/tutorial.html)

### 10.2 如何避免同事实泄漏

不建议把所有题随机打散后就结束划分。来自同一段落、同一表格或同一事实的近似问题，应分到同一组。

可依据文档、主题或事实簇分组，再兼顾题型、语言和表格比例。小规模语料下可能需要折衷，具体方案应在报告中记录。

**保留问题不等于必须从索引删除对应文档。**常规 RAG 选型是在同一知识库中保留一部分查询不参与调参。如果另做“新文档泛化”实验，应明确将文档也划分，这是不同问题。

### 10.3 小数据集的解释边界

若保留集很小，几个题目的波动可能改变平均分排名。需要同时查看逐题结果、错误类别和配置间差值；必要时对接近的候选复跑，或补充针对缺失场景的独立问题。

可考虑按文档/主题簇 bootstrap 等方法报告不确定性，避免把同文档问题视为完全独立。这是后续统计增强建议，当前未实现。

## 11. 到底评测什么：检索层与回答层

### 11.1 两层都需要，作用不同

**检索层**检查：是否召回了支持答案的内容、排序是否合理、是否需要大量上下文才能找到证据。

**回答层**检查：模型是否给出正确完整的答案、是否忠于提供的证据、有无编造、耗费多少 token 与时间。

只看检索不足以保证最终回答好；只看最终回答又可能掩盖模型使用自身知识补齐检索缺失。对于分块选型，需要检索与回答两层评测。“先筛检索、再对入选候选评回答”可节约运行成本，但不是必须步骤；本项目首轮小网格建议对所有有效候选完成两层评测，具体规则见第 11.6 节。

### 11.2 Ragas 指标的准确含义

| 指标 | 主要输入 | 测什么 | 不能据此直接断言什么 |
|---|---|---|---|
| LLM Context Recall | `user_input`、`reference`、`retrieved_contexts` | 参考答案的主张有多少得到检索上下文支持 | 不等于原文字符区间覆盖率，不是传统 chunk ID recall |
| Context Precision（有参考答案版本） | 问题、参考答案、排序后的检索上下文 | 有用上下文是否更靠前，采用 AP 类排序计算 | 不等于有用字符占全部字符比例，也不直接衡量大块噪声密度 |
| Factual Correctness | `response`、`reference` | 回答与参考答案的事实一致性，可采用 precision/recall/F1 模式 | 不保证这些事实都来自已提供上下文 |
| Faithfulness | `response`、`retrieved_contexts` | 回答中的主张是否受到检索上下文支持 | 不保证回答完整；遗漏关键事实的短答案也可能很忠实 |

来源：[Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)、[Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)、[Factual Correctness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/)、[Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)。

不同版本和指标变体的必需字段可能不同，应在接入时按选定版本确认，记录具体类名、模式和提示词。不能把上表当作无需核对 SDK 版本的直接调用代码。

### 11.3 为什么 Context Precision 不能代替上下文噪声测量

AP 类计算主要关注相关项的排序。如果有用块已经排在前面，末尾加入一些无关块时，分数不一定明显下降。一个同时含证据和大量无关内容的大块，也可能被判为有用块。

因此需要同时记录实际上下文 token 数、重复率、证据覆盖、截断及最终回答质量。不得声称“Context Precision 高，所以大块没有噪声”。

### 11.4 建议的第一阶段指标组合

建议先使用：

1. **检索主指标：**LLM Context Recall。
2. **检索排序辅助：**Context Precision。
3. **回答质量：**Factual Correctness，明确选用的模式。
4. **上下文忠实性：**Faithfulness。
5. **运行指标：**实际上下文 token、检索/回答/评分延迟、调用次数及成本。
6. **分层报告：**本轮按正文题型和语言报告；表格专项已暂缓，不把原 9 道表格题混入首轮均分。标题依赖等额外类别需有实际标注后再报告。

最终验收规则应在正式搜索前明确。没有讨论并确定阈值前，不能写出“得分超过 0.8 即合格”等任意门槛。

### 11.5 LLM 评审并非人工真值

LLM 评分会受模型能力、语言、提示词和运行随机性影响。建议对一组有代表性的题保留人工判定，检查 LLM 与人工是否一致，并重点核对表格、短答案、多证据和临界分数样例。[Ragas 评审模型对齐](https://docs.ragas.io/en/stable/howtos/applications/align-llm-as-judge/)

同一实验使用相同评审模型、版本、提示词和设置；保留逐题判断与失败信息。得分很接近的候选需要进一步核对，而不是仅比较最后一位小数。

### 11.6 “较好的候选”如何定义：先写规则，不能事后挑赢家

这里的候选是一套完整配置，例如 `strategy=B, chunk_size=512, overlap=51, length_counter=...`；B/C 两种策略都可能有多个参数候选。此前“对较好的候选生成回答并用 Ragas 评分”缺少保留规则，属于不完整的实施描述，并不代表当前已经有一份筛选标准或赢家名单。

检索阶段可以使用 Ragas：基于 LLM 的 Context Recall 使用问题、参考答案及召回内容；有参考答案的 Context Precision 判断召回块的有用性与排序。它们不要求先生成当前 RAG 系统的回答，但仍会调用评审模型，不能称作免费离线筛选。[Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)、[Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)

**本次复核后的首轮建议：所有有效且不重复的候选都完成检索与回答评测。** 候选草案为 18 组，按用户暂缓表格的最新范围决定，正文调参集为 24 题；每种检索/预算视角最多对应 432 次回答生成，另有多次 judge 调用，实际费用必须根据选定模型和上下文估算。两种视角均独立生成回答时，工作量还会增加。这是规模估算，不是已批准运行的清单或费用。相同分块内容/顺序与索引可复用计算，但须保留配置对应关系，不能因均分低而把该配置结果隐藏。

进入比较的条件是实现有效：语料和 QA 哈希一致、分块参数合法、最终文本长度经检查、索引不混入其他配置、模型调用和评分可追溯。来源无法映射、超限或接口失败应记录并修复/报告，不能包装成低质量候选再随意排除难题。不同配置必须基于相同题集或明确的配对有效题集比较。

若以后候选很多、成本要求分阶段筛选，**运行前**另写筛选协议：

1. 只用调参集和同一检索/上下文规则；不使用保留集筛选。
2. 并列看 Context Recall、Context Precision、实际上下文长度、失败数，以及中文/英文、不同正文题型的逐题表现。
3. 可保留质量与成本之间有不同优势的候选，例如没有被其他配置同时以更高质量、更低成本超过的候选；同时为 B/C 均保留深入比较机会。具体保留数量、容许退步与成本限制需提前登记。
4. TF-IDF 百分比、单一字符覆盖率或某一个参数点的均分不能作为正式筛选依据；不能直接规定未经验证的“0.8 即合格”。
5. “进入下一阶段”仅表示值得评回答，不代表已证明回答更好。最终还要看 Factual Correctness、Faithfulness、逐题错误和运行成本；分数接近时复核并报告不确定性。

上述全量评测/分阶段筛选是项目实施建议。Ragas 提供指标，不替我们规定候选保留数、业务门槛或最终质量与成本取舍。当前尚无已冻结的数值验收阈值。

## 12. 原文证据覆盖：来源、公式与适用条件

### 12.1 依据来自哪里

之前提出的“将每个检索块映射回原文，计算 gold evidence 被覆盖的比例”与 Chroma 的分块评测技术报告及开源实现有关。该工作使用查询、证据片段和来源区间，评价检索内容的 Recall、Precision、IoU。[Chroma 技术报告](https://www.trychroma.com/research/evaluating-chunking)、[开源实现](https://github.com/brandonstarxel/chunking_evaluation)

需要明确三点：

- 它是一种公开的分块检索评测方法，不是 RAG 行业统一标准。
- Azure 准备阶段文档支持保存证据与页码，但不是该公式的直接来源。
- Chroma 报告中的描述与具体代码的计量口径需要区分；源码使用来源字符区间等计算，不能把不同实现一概称为严格 token 级集合指标。

源码参考：[base_evaluation.py](https://github.com/brandonstarxel/chunking_evaluation/blob/main/chunking_evaluation/evaluation_framework/base_evaluation.py)。

### 12.2 本项目可选的区间覆盖定义

设：

- `G_d`：在同一规范文档版本 `d` 中，审核后的 gold evidence 区间并集。
- `R_d@k`：前 k 个检索块在该文档中的可靠来源区间并集。
- `|.|`：所选计量单位下的长度，需明确是字符还是 token。

则建议的**原文证据字符覆盖率**为：

```text
EvidenceCharCoverage@k
  = Σ_d length(G_d ∩ R_d@k)
    / Σ_d length(G_d)
```

这属于基于公开区间方法的项目适配定义。对于同一文档，重叠区间先合并，避免同一证据因多个重叠块被重复计数；不同文档或版本的 offset 不可混算。

### 12.3 这个指标回答什么，没回答什么

可以诊断：

- 一段证据被切开时，多个块是否共同召回了它。
- 某个方案是否只召回证据的一小部分。
- 在同样上下文预算下，证据覆盖是否提升。

不能单独判断：

- 答案是否正确和完整。
- 证据中每个字符的语义重要性是否相同。
- 表头缺失是否使整个表格值失去含义。
- 同义表述或另一段充分证据是否同样能支持答案。

如果存在“证据 A 或证据 B 任一都足够”，应记录可替代证据组；如果必须同时具备 A、B，应记录联合要求。把所有可替代证据硬拼成一个必须全覆盖的集合会低估召回。这是项目标注设计，需要在启用该指标前明确。

### 12.4 启用前置条件

1. 文档版本固定且有哈希。
2. gold evidence 可准确回源。
3. 分块过程中保留可靠的来源映射。
4. 人为注入标题、摘要等文本不伪装成原文区间。
5. 无法映射的块显式记录，报告缺失比例。
6. 实际给模型的裁剪后上下文也能映射；不能只对裁剪前候选计算覆盖。

在这些条件满足前，先使用 Ragas 的语义支持指标和人工诊断，不使用当前弱 `start/end` 冒充精确覆盖。

### 12.5 不再作为主指标的旧判定

```python
answer[:60] in chunk.content
```

这个判定会受到答案改写、证据跨块、前缀不唯一、标题注入和空白变换影响。某题即使召回了全部必要证据，也可能因为答案字符串没完整出现而被判未命中；反过来，命中一个常见前缀也未必已得到充分支持。

它可作为局部字符串检查工具，但不应再作为策略效果的主要命中依据。简单模糊匹配也不能自动解决这些语义和来源问题。

## 13. 如何保证不同分块方案比较公平

### 13.1 固定哪些东西

同一轮对比应固定并记录：

- 语料与 QA 版本。
- embedding 模型、版本、维度、文本预处理和相似度计算方式。
- 查询语言与查询改写规则。
- 检索方法、过滤条件和 reranker 设置。
- 回答模型、提示词、输出限制和上下文拼装规则。
- 评审模型、指标版本、提示词和运行设置。
- 上下文预算、去重、截断和邻块扩展规则。

一次改变多个变量时，必须将其标为不同完整系统配置，不把全部收益归到“分块策略”。

### 13.2 只固定 top-k 还不够

同样 top-5，B 可能给模型提供大量字符，C 可能只提供较短片段。较大的块通常更容易覆盖证据，同时增加成本和噪声。

建议报告两种视角：

| 视角 | 规则 | 用途 |
|---|---|---|
| 固定 top-k | 相同 k 下检索，不忽略实际 token 数 | 看排名和每块信息差异 |
| 固定上下文 token 预算 | 相同拼装规则和预算，保留最终真正使用的内容 | 比较相近资源条件下的效果 |

这是提升本项目比较公平性的工程建议，不是某一个框架强制规定的统一规则。

### 13.3 拼装和截断必须显式记录

候选检索结果与实际模型上下文应分开：

```text
retrieved_candidates
  → 排序 / rerank
  → 去重或合并（如果启用）
  → 按预算打包 / 截断
  → contexts_used_for_generation
```

评测回答支持度时，应传实际使用的上下文。若检索阶段指标使用的是全部候选，应明确标成候选检索指标，不能据此说明回答模型已经看到了这些证据。

标题前缀、表格表头重复和文档标签都占 token；不能从预算中悄悄排除。若表格不允许中途裁剪，应固定规则并记录未装入的原因。

### 13.4 第一轮尽量控制实验维度

建议先固定 embedding、retriever、reranker 和回答设置，只比较 B/C 及少量明确参数。后续再研究混合检索、rerank、父子块或相邻块扩展。

当语料规模较小时，可使用精确余弦计算作为简化检索实现；也可接入项目已有向量库。M3 已选择 L2 归一化后的精确余弦排序，同分按 chunk ID 排序；后续比较应保持一致，不将后端差异混入分块结论。

### 13.5 固定模型与检索设置如何确定：本轮的具体建议

本轮要回答的是：在一套明确的 RAG 环境下，哪种分块配置更合适。先根据语料语言、可用接口、输入限制和成本选择可用的控制环境，进行小样本接口与能力检查，然后在整轮分块比较中保持一致。无需先求出“全行业最好的 embedding/回答模型”才能开始。更换模型或检索器时属于另一轮系统配置/稳健性比较，不把变化混进本轮分块收益。

2026-10-09 回读本地活动配置得到 `text-embedding-v3` 和 `qwen-plus`，并完成 M3 接入；这段只描述历史 M3/M4，不能代表后续新轮次。后续新轮次读取到的活动 profile 为 `deepseek-v4.1-flash` + `text-embedding-v3`，新的 QA 生成、回答和 judge 统一使用 DeepSeek，旧 qwen 运行结果不重算、不改名、不混入新结果。模型设置、缓存隔离和同模型 judge 的偏差边界见 [模型设置说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/MODEL_POLICY_2026-10-12.md)。

| 部分 | 首轮建议 | 如何决定与验收 |
|---|---|---|
| embedding | 先接入现有 `proxy/tongyi` 的 `text-embedding-v3` | 检查中英文输入、返回向量维度/有效值、文档与查询的相同处理、输入限制；记录维度和归一化规则 |
| 回答模型 | 先接入现有 `qwen-plus` | 用相同中文/英文及不同正文题型样例检查基于上下文作答；固定提示词、输出长度、支持的采样设置及不足证据处理规则 |
| judge | 独立登记一套模型设置；可先验证现有可用模型 | 检查所选 Ragas 版本要求的结构化输出、事实拆解与中英文判断；人工核对有代表性的评分。回答模型与 judge 可相同，但需记录并检查自评偏差，不能直接认定可信 |
| 检索器 | embedding 后精确余弦排序 | 当前语料小，避免近似索引搜索误差；固定归一化、同分排序与异常处理，各配置使用独立索引 |
| top-k | 初始使用 5 | 复用历史起点，便于衔接诊断；不是已验证最优值。整轮统一，另可利用已缓存排序查看 k 的敏感性 |
| 查询和来源范围 | 原问题，全体冻结文档 | 不按 gold `doc_id` 过滤，不加查询改写、混合检索、rerank、相邻块扩展；这些变化后续另评 |
| 上下文拼装 | 排名顺序、统一去重/预算/裁剪规则 | 同时报告固定 k 与固定预算；传给回答及 Faithfulness 的是实际使用内容，保留未装入/裁剪的原因 |
| 指标 | Context Recall、参考答案版 Context Precision、Factual Correctness、Faithfulness，加用量/延迟/失败 | 固定 Ragas 版本、具体类/API、Factual Correctness 模式、提示词、judge 及重试设置；以小样本核对后才用于批量比较 |

**上下文预算不能只写“相同”而不写具体数值和计量器。** 先确认回答模型上下文上限，扣除 system prompt、问题、标签、预留输出和接口开销，得到可用范围；再结合候选实际块长和计划成本选一个共同预算，搜索前写入配置。标题前缀、重复表头等均占预算。若拿不到回答模型的真实 tokenizer，明确写“相同本地计量 token 预算”，记录 provider 用量；不要把它描述成精确相同的模型实际 token 数。固定 k 结果中的实际上下文长度差异也必须展示。

M3 至少交付以下可审查内容后，才能称实验设置已经固定：

1. corpus/QA 版本、使用 split 及审核状态；小样本从调参集取，最终确认不使用已暴露的旧保留集。
2. embedding、回答、judge 的 provider/模型标识、可用版本、维度及模型接口配置；不含凭据。服务只有可变别名时记录返回标识和运行日期，说明没有锁定权重。
3. 本地长度计量器及版本、分块实现/边界、标题规则、size/overlap 单位与取整，最终块长度检查。
4. 相似度、top-k、来源范围、查询规则、预算、去重/裁剪和实际使用上下文。
5. 回答/指标提示词与版本、采样/输出限制、Ragas 版本及指标变体、失败/重试/缓存规则。
6. 候选清单与选择理由、是否全量端到端评测、最终质量/成本比较规则及费用估算。没有业务数值门槛时如实标为探索性比较，不能声称正式上线验收通过。

上述最小闭环设置、配置/依赖/代码哈希及输入检查已在 M3 保存。M4 现已保存 v1 候选与比较协议，人工核对仍 pending。M3 暴露指代假阴性、事实拆解添加含义及步骤关系遗漏，后续 21 例控制也显示仅修改严格拆解提示词未修好关系判断。详见 [M3 检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/agent_review.md)、[控制检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/judge_calibration_v1/agent_review.md)。M4 补充按题评分、保留官方诊断；几题或未经人工校准的小数差异不能选择最终方案。

## 14. 块大小与重叠参数如何确定

### 14.1 先澄清旧候选值的性质

此前提出：

- `chunk_size = 400 / 800 / 1200 / 1600`。
- overlap 比例 `0% / 10% / 20%`。

这些是探索性建议，**不是行业标准，也不是已经确定的本项目搜索空间**。没有经过当前候选语料的 token 统计、题型分析和实验验证，不能说这些数值有特殊最优性。

当前代码默认 `800/100` 也只是历史实现配置，不是选型结论。

### 14.2 官方示例说明什么

Azure 分块文档给出 512 tokens、约 25% 重叠作为起点，并说明最优重叠依内容而变。该建议不能直接等同于 512 个中文字符。[Azure 文档分块](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents)

LlamaIndex 的参数优化示例包含 `[256, 512, 1024]` 等块大小及不同 `top_k`；另一个官方案例尝试多个块大小，比较回答质量和耗时。示例证明可以做候选搜索，未证明这些数值在我们代码的字符计量下同样合适。[参数优化示例](https://developers.llamaindex.ai/python/examples/param_optimizer/param_optimizer/)、[块大小评估案例](https://www.llamaindex.ai/blog/evaluating-the-ideal-chunk-size-for-a-rag-system-using-llamaindex-6207e5d3fec5)

### 14.3 搜索空间的实际制定步骤

1. 统计冻结语料的正文段落、章节及全文长度分布；本轮不以表格专项统计制定参数。
2. 按明确选定的本地 tokenizer 记录 token 数，观察中文和英文差异；区分本地计量与 provider 真实用量。
3. 确认 embedding 单条输入限制与回答模型上下文限制。
4. 确认每题可用的检索上下文预算。
5. 观察初始 B/C 的实际块长分布、超长率、碎块、正文证据切断与来源映射。
6. 选覆盖短、中、长几个量级的粗搜索点，并记录选择理由。
7. 看有效区间后再做局部细化；若最佳值处于搜索边界，考虑扩展范围。

**当前已接受的路线：正式分块采用 token 计量。** 先固定 embedding 模型及明确的本地 tokenizer，再使用统一 `length_function` 或等价的 token 约束；保留段落、句子、空格等递归边界。本地计量器可以与 embedding 的 provider tokenizer 不同，但不得混淆其含义，模型输入限制需另行核对。递归切分的边界类型与块长度计量单位是不同概念，B 的算法名称并不强制块大小必须按字符计。

tokenizer 名称、版本、特殊 token 处理、chunk_size / overlap 单位都写入配置。C 标题注入计入最终块长，token 计数不保证可相加，直接检查完整拼接文本。上下文另登记计量器与预算；旧字符运行不能仅改标签转成 token 实验。现已对 `B/C × 256/512/1024 × 0/10/20%` 做本地计量/块形态检查，向下取整，18 声明配置均通过，16 独立块集合；选择理由与预算/比较协议见 [M4_PROTOCOL.md](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_PROTOCOL.md)。它们是本项目粗搜设计，不是行业统一标准或最终参数。

当前活动配置文件为 `/Users/zsm/.dbgpt/configs/qwen.toml`，其中 embedding 候选是 `text-embedding-v3`、提供方为 `proxy/tongyi`。这只是运行环境线索，不等于正式实验已冻结该模型。`pilot/.venv` 当前没有 `tiktoken`、`transformers` 或 `tokenizers`；后续选一个依赖固定的环境与明确本地计量器，不混用多个环境，不把字符数改名为 token 数。

### 14.3.1 已接入的长度计量入口

实现位于 [`token_measure.py`](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/token_measure.py)，并已接入 [`evaluate_chunking.py`](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/evaluate_chunking.py) 的 `--length-unit` 参数：

| 后端 | 用途 | 规则 |
|---|---|---|
| `characters` | 旧链路回归与实现冒烟 | 明确记录为 Python `len`，不能解释为 token |
| `tiktoken` | 已知模型对应计量，或明确声明为独立的本地 token 计量口径 | 必须传 `--tiktoken-encoding` 或已知的 `--tiktoken-model`；未知模型不猜 encoding，不把独立计量宣称为 provider 实际 token |
| `transformers` | 选定公开 Hugging Face tokenizer 后的本地计量 | 必须传 `--tokenizer-name`，记录库版本、revision 和 `add_special_tokens=False` |

三个后端都会把计量后端、标识、库版本、特殊 token 规则和单位写入输出配置。token 后端缺少依赖或 tokenizer 时直接失败，不会把字符数静默改成 token 数。当前已运行 `characters` 回归：B 生成 74 块，C 生成 116 块；C 先扣除标题前缀预算，最终块长度均不超过 800 个 Python 字符。这是实现回归，不是正式 token 或效果结论。另用明确指定的 `cl100k_base` 做过一次 token 后端冒烟；它不能被称为 `text-embedding-v3` 的官方 tokenizer。现有冒烟没有正式语义检索/回答/评分，因此不能用于策略排名；若今后将其作为明确冻结的本地计量器，可以运行新的受控实验，并按该计量器报告参数与结论。

当前还没有为 DashScope `text-embedding-v3` 冻结一个官方 tokenizer。不能用 `cl100k_base`、Qwen tokenizer 或字符近似替它宣称“真实 token 数”；需要先接受一种可复现的计量方案，或改选带公开 tokenizer 的 embedding 模型并同步冻结 embedding 配置。

**校准此前过度限制：保留 `text-embedding-v3` 不要求开发 provider 计数驱动的自适应分块器，也不必为了 tokenizer 更换 embedding。** 固定本地计量口径、检查最终块长、审计 provider 用量/输入限制，足以支持明确注明计量与模型条件的受控比较。provider 审计是模型侧用量检查，不自动成为分块算法；全文计数也不能替代段落、章节、表格及证据长度分布。

官方 DashScope embedding 响应提供 `usage.total_tokens`，并说明这个数由模型 tokenizer 对输入解析后得到；`text-embedding-v3` 单条输入上限为 8,192 tokens。它可以作为 provider 侧的权威审计值，但批量请求返回的是整个请求的总数，不能直接得到每个输入的独立计数。因此，若继续使用该模型，实际方案应是：用已接受的本地计量器做切分，再用单文本 provider 调用抽样校准/审计；或者改用公开 tokenizer 与 embedding 模型一致的模型。不能把一次批量总数平均分配给各块。[Alibaba Model Studio embedding API](https://help.aliyun.com/en/model-studio/text-embedding-synchronous-api)

已先对 4 份短、中、长及中英文混合文档做接口验证，随后对 14 份纳入文档做单文本 provider 审计。完整产物为 [`provider_token_audit_full_v1.json`](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/provider_token_audit_full_v1.json)，4 份小样本记录仍保留在 [`provider_token_audit_v1.json`](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/provider_token_audit_v1.json)。14 份文档共 12,128 provider tokens，p50 为 692，p90 为 1,352，最大为 3,128；provider 与 `cl100k_base` 的比例范围为 0.844937–1.289634，不能用一个固定字符/token 比例解释中英文混合语料。

### 14.4 重叠参数如何考察

重叠可能减少边界处的信息缺失，但会增加重复内容、索引规模和上下文消耗。

可以包含零重叠对照，再探索少量非零设置；具体比例或字符数待语料分析后确定。必须记录：

- overlap 是绝对字符、token 数还是按块大小换算。
- 换算、取整规则。
- 实际重叠长度，而非只写配置值。
- 重叠是否跨章节、表格或其他元素。
- 重复召回对上下文预算的影响。

### 14.5 策略与参数需要联合考虑

仅在一个参数点比较 B/C，然后淘汰输的一方，可能错过其更合适的参数区间。

建议：

```text
初始对比：B/C 在清楚定义的起始设置下检查实现和明显问题
  → 粗搜索：两个策略都保留，尝试各自合理参数范围
  → 细搜索：在较好的区域进一步比较
  → 保留集：验证最终候选
```

初始对比用于建立基线和观察问题，不必立刻宣布最终冠军。

### 14.6 C 的专属参数与后续消融

除块大小、overlap 外，C 还可能考察：

- 登记到 H3 还是 H4 等。
- 是否注入标题，注入哪些祖先标题。
- 小章节是否合并、什么条件下合并。
- 是否保护表格元素、跨页/跨段表格如何处理。

建议先保留当前实现作为明确起点，再一次只引入可解释的变化。不能把标题注入、层级调整、表格保护同时改完，却只报告“结构感知优于递归”。

## 15. 表格专项暂缓：首轮正文评测范围

用户明确表格不是重点，首轮先不考虑表格。当前 B/C 不识别独立表格元素，不保护表头与表体，也不做行组分块或重复表头；Markdown 表格沿普通文本路径处理。首轮不增加这些功能，不补充表格题，不用表格题调参或给出表格能力结论。

**本轮实际输入：**[qa_text_only_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_text_only_v2.jsonl)，从完整 v2 的 44 题中排除 `table_dependent=true` 或 `question_type` 以 `table_` 开头的 9 题，保留 35 题、覆盖原 14 份文档，其中英文 27、中文 8，调参 24、原保留 11。保留题按原行复制，ID、问题/答案、证据、审核状态和原 split 不变。

[qa_text_only_v2_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_text_only_v2_manifest.json) 保存父集/子集哈希、范围 ID、规则和纳入/排除题目。原 44 题、原审核材料和原版本 manifest 保留，避免历史结果失去版本依据。14 份语料继续使用原哈希与原文，表格文本留在索引中照常切分；“正文评测”不表示从源文档删表格。人工内容审核仍待完成，11 题原保留集也仍不具备未暴露盲测性质。

M3/M4 必须显式读取正文子集并选择 tuning split；历史 TF-IDF runner 的默认完整 QA/全部 split 行为不等于本轮正式评测配置。当前模型 runner 已保存 `scope_id`、QA 文件哈希、所选 split 及参考答案修订配置。以下 15.1–15.3 作为将来恢复表格专项的参考，**不是本轮任务或验收要求**。

### 15.1 解析正确是前提

表格题需要审核原始表格与规范文本一致，至少确认：表头、行名、列名、数值、单位、日期及脚注。解析错误导致答案错，应归到解析层，而非仅归到分块策略。

现有无代码表格 Markdown 可先纳入经过审核的小表。复杂 PDF 财务表格另设解析改进与评测阶段。

### 15.2 不先把改进能力写成现状

当前 B/C 直接对 Markdown 文本切分，不能保证整张表或表头与每行始终同块。需要记录这一现状，并检查其对表格题的影响。

后续可比较的设计包括：

1. 小表作为一个元素保留，并附标题、说明和单位。
2. 大表按行组切分，每组重复必要表头和适用说明。
3. 原表结构保存为 HTML/JSON 等，索引中使用可检索文本或描述，召回后通过稳定 ID 回取原表。
4. 对精确聚合、计算题使用结构化查询工具，再把结果交给模型解释。

这些是后续候选方案，不能在本轮默认全部启用。表格序列化、重复表头和回取策略发生变化时，也应记录为实验变量。

### 15.3 表格子集的错误分析

建议将错误细分为：

- 没找到表格。
- 找到表体但缺表头。
- 找到正确表格但缺目标行/列。
- 多行比较只找到其中一行。
- 单位、脚注或适用条件缺失。
- 原始解析已错列/错值。
- 检索正确但模型读错单元格或计算错误。

这样能判断下一步应改解析、分块、检索还是回答提示，而不是把所有错误都称为“召回失败”。

## 16. 批量实验怎么实现和记录

### 16.1 拟议流程

```text
读取 corpus manifest + 已审核 QA
  → 读取当前候选配置
  → 分块并保存 chunk 清单、结构和长度统计
  → 对实际 embedding_text 向量化
  → 为当前配置构建独立索引
  → 用固定查询检索
  → 记录候选、排序、上下文拼装和最终上下文
  → 进行检索指标评测
  → 对进入端到端比较的候选生成回答
  → 用统一评审设置评分
  → 保存逐题结果、异常和汇总
  → 比较配置，人工检查失败案例
```

这是待实现流程，本次文档更新没有运行上述实验。

### 16.2 每次运行要保存的配置

| 配置组 | 至少记录 |
|---|---|
| 数据 | corpus、QA、split 版本与哈希 |
| 分块 | 策略、实现版本、长度单位、size、overlap、headers、inject、表格规则 |
| 向量化 | provider、模型及版本、维度、前处理、归一化、batch 规则 |
| 检索 | 索引后端、距离函数、top-k、过滤、rerank、扩展规则 |
| 上下文 | token 预算、tokenizer、去重与裁剪规则 |
| 回答 | 模型版本、提示词版本、温度等设置、输出限制 |
| 评测 | Ragas 版本、具体指标及模式、judge 模型、提示词、运行参数 |
| 执行 | run/config ID、时间、代码版本、随机种子（支持时）、重试规则 |

API key 仅通过环境变量或本机凭据配置提供，不写进配置快照、结果文件和文档。

### 16.3 每题要保存的结果

- `question_id`、`run_id`、`config_id`。
- 检索到的 chunk ID、文档 ID、分数和排序。
- 实际用于评分或回答的上下文文本。
- 可用的可靠来源区间与标题路径。
- 去重、裁剪、未装入的块及原因。
- 回答文本及模型使用量。
- 每个指标的分数、必要判断信息和异常状态。
- embedding、检索、回答和评分的独立延迟、调用数及成本。
- 人工失败类别与备注。

只保存均值不够。需要能从一项低分追溯到原问题、证据、检索块和模型回答。

### 16.4 缓存与失败处理

建议对确定性输入缓存 embedding；键包含实际文本、模型/版本、维度及相关前处理参数。不同配置中完全相同的文本可以复用，但标题注入不同不能错误命中旧缓存。

固定查询向量可在设置一致时复用。不同候选索引和 chunk 元数据必须保持隔离，避免混入其他配置的块。

LLM 评测缓存需包含问题、参考、上下文、回答、指标与 judge 设置等输入，不能只按 `question_id` 缓存。

出现失败时：

- 保留错误，区分限流、超长、解析失败、模型输出不合法等原因。
- 重试规则对各配置一致。
- 不将未评分样本静默记为 0。
- 不让不同配置各自删除不同的难题后再比较均值。
- 汇总有效样本数、失败数，以及可进行配对比较的问题集合。

### 16.5 搜索器不是第一步的必需品

候选数量少时，一个读取配置列表、逐配置执行并保存结果的 runner 即可。Optuna 可以后续接入，但其目标函数仍需要我们构建 RAG 流程并返回可信分数。[Optuna](https://github.com/optuna/optuna)

如使用复合目标，应预先说明质量、成本、延迟如何权衡。未确定权重时，优先并列展示这些指标，不用任意加权公式制造一个“总分”。

## 17. 如何解读结果并选方案

### 17.1 不只看平均数

每个候选至少报告：

- 总体和各题型得分。
- 调参集与保留集表现。
- 同题配对差异。
- 上下文 token、索引块数、向量化成本与运行延迟。
- 失败和未评分样本数。
- 典型改善与退步案例。

建议逐题比较：B 能回答而 C 不能的题、C 改善的题、两者都失败的题。检查错误是来源于结构分块、切断、检索排序、上下文截断、解析错误还是模型回答。

### 17.2 方案选择规则应提前约定

可能的取舍：

- 在相同上下文预算下，选择质量更稳定的候选。
- 质量接近时，选择成本更低、实现更简单或来源更容易追溯的方案。
- 表格和正文表现差异大时，可研究按文档类型选策略，但需要独立验证。

这些是决策原则建议，不是已经选择了某个赢家。若差异小于评分波动，结论应是“目前证据不足以区分”，随后补充诊断或复跑。

### 17.3 正式结论的表达模板

```text
在 corpus_version=...、QA_version=... 的范围内，
使用 ... embedding、... 检索方式、... 上下文预算、... 回答/评审模型，
比较 B/C 及其 ... 参数候选。

推荐配置为 ...，依据为 ...。
相对另一个候选，在 ... 类型上改善，在 ... 类型上有退步或不确定性。
成本、延迟、失败样本情况为 ...。
结论尚未覆盖代码块、复杂 PDF 表格和图片多模态场景。
```

不能将这类局部结果推广为“结构感知分块在所有企业 RAG 中都更好”。

## 18. 开源复用与自研边界

| 工作 | 直接复用的基础 | 本项目仍需完成 |
|---|---|---|
| QA 候选生成 | Ragas TestsetGenerator、语言适配 | 文档输入、中文提示、来源关联、人工审核 |
| QA schema | Ragas SingleTurnSample | 版本、来源、题型、审核与 split 元数据 |
| 指标评分 | Ragas 指标 | 模型适配、输入转换、版本固定、中文校准 |
| 实验入口与记录 | Ragas quickstart 等模板 | B/C 配置循环、索引隔离、逐题输出、汇总 |
| 参数搜索 | 简单循环或后续 Optuna | 搜索空间、目标函数、成本规则 |
| 原文区间评测 | Chroma 方法和代码参考 | 准确映射、gold 标注、重叠/替代证据定义 |
| 语料清洗 | 现有脚本 | 规则审核、来源保留、冻结 manifest |
| 表格分块 | 公开元素保留/行组等思路 | 具体实现及对照实验 |
| 正式报告 | 框架输出和项目日志 | 失败分析、统计解释、适用边界和最终决策 |

因此，“评测集需要自己准备”是指必须提供并审核本领域数据，不能靠通用框架自动保证质量；“批量脚本需要自己实现”是指需要适配现有链路，框架模板可以减少重复工作。

## 19. 下一步实施里程碑与验收产物

### M1：冻结第一阶段语料（已完成暂定 v1）

**工作：**审核 28 份 Markdown 候选，暂定纳入 14 份、暂缓 14 份；如补充 Word，另记录解析与审核情况。

**产物：**[corpus_manifest.json](/Users/zsm/Documents/DB-GPT/pilot/data/eval/corpus_manifest.json)、[_corpus_review.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/_corpus_review.md)，含冻结名单、文档哈希、纳入/排除理由、结构/表格/图片统计和边界说明。

**验收：**已能明确回答“QA 生成阶段读哪些文件、为什么选它们、版本是什么”。这仍不是事实级 QA 审核或正式实验语料认证。

### M1b：中文长文 canonical v1 冻结（已完成；2026-10-12）

针对新筛出的 29 份中文企业合规候选，已完成固定 commit、原始哈希、front matter、正文完整性、标题/条款结构和官方 URL 记录的结构性审核。规范化只移出 YAML front matter、12 个明确的目录导航块和首尾格式空白；没有补造 ATX 标题、改写条款、合并不同版本或把同名替代来源混入主体库。每篇规范 Markdown 都有 metadata sidecar 和 raw→canonical 字符区间映射。

产物：[canonical manifest](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/canonical_manifest.json)、[逐文档审核报告](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/audit_report.md)、[规范文本目录](/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/zh_enterprise_compliance_canonical_v1/docs)。

验收边界：29/29 份通过 RAG 语料质量检查，状态是 `canonical_frozen_rag_quality_reviewed`。本轮关注文本完整性、编码、清洗可追溯性、分块可用性和来源映射；不做法律效力认证。正式 QA 前只需审核问题、参考答案和支持证据是否正确。3题 Ragas 小批只用于适配检查；随后生成的30条 v3 候选已完成条款级回源检查，但仍未改变正式 QA 的冻结状态。

### M2：建立已审核的固定 QA 集

**状态：**已完成 seed/provisional v1 及 44 题逐题代理回源复核 v2；人工内容审核尚未完成。

**已有产物：**[qa_source_reviewed_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_source_reviewed_v2.jsonl)、[qa_review_packet_v2.md](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_review_packet_v2.md)、[qa_audit_v2.jsonl](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_audit_v2.jsonl)、质量报告与版本 manifest。15 题保留、29 题修订；中文文档的 13 题改为中文；9 道表格题参考上下文均含表头。

本批 QA 来源是 Codex 代理根据冻结 Markdown 编写，不是人工编写或真实用户日志。v1 数据中的 `manually_authored_from_frozen_source` 属错误来源标注，保留历史文件但不再沿用；v2 已更正。脚本没有发起额外的模型 API 调用。q0005、q0036、q0037 更换考查点来处理重复事实，运行必须带 QA 版本，不能仅按旧 ID 合并结果。

v2 的 `source_spans` 是选定支持证据；`reference_context_spans` 可以额外包含完整表格与周边说明。两类区间均能从冻结规范原文还原；表格无关行不计入支持证据分母。证据 ID 用完整文档哈希和位置区分，真正共享同一源区间才共享 ID。其他有效证据未必被穷举，覆盖率仍只能作辅助诊断。

**剩余验收：**人工确认问题、答案及证据，修订另存新版本并记录审核人和哈希。原 14 条保留题已经用于历史诊断，不具备未暴露盲测性质；最终确认需新的独立材料。44 题也不等于 44 个统计独立事实，相关问题及跨文档主题仍需在分析中考虑。

### M2b：canonical v1 上的 Ragas 候选 QA 小批（已完成试跑，待人工审核）

为验证生成器适配和审核链路，已从 `zh_enterprise_compliance_canonical_v1` 选取 3 份中文长文，实际运行 Ragas 0.4.3 `TestsetGenerator`，生成 3 条单跳候选。运行保存了 Ragas 原始测试集、生成配置、provider 事件、完整 `reference_contexts` 以及候选元数据。

产物：[生成配置与运行元数据](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v1/generation_config.json)、[原始候选](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v1/qa_candidates.json)、[代理回源审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v1/review_packet.md)、[代理审核结果](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v1/qa_candidates_agent_reviewed.json)。

本批发现了生成器需要人工修订的实际问题：一条问题混入无关的年假前缀，另一条是关键词片段而非自然问句；代理已保留原始输出并另存改写，不把改写结果当成人工批准。3 条候选均已映射到 canonical evidence spans，但仍需逐题核对官方正文、版本、适用对象、条件/例外和答案完整性。当前状态是 `candidate_only / agent_reviewed / human_approval_pending`，不能用于新的 B/C 分块排名。

### M2c：canonical v1 的全量候选 QA v3（代理语义核对完成，正式冻结待做）

在小批适配确认后，使用新的 DeepSeek 模型配置和确定性逐文档覆盖适配，从 canonical v1 的全部 29 份文档生成 30 条 single-hop specific 候选。该适配只替换 Ragas 0.4.3 在中文 persona 名称映射上的脆弱场景选择，问题/答案仍走官方 single-hop synthesizer；旧 Qwen 小批、失败的 v2 适配目录和新 v3 结果分别保存，不能混用。

v3 已完成 30/30 条 canonical SHA、完整 `reference_contexts`、source path、mapping sidecar 和证据区间校验，覆盖 29 个文档。逐题语义核对后，26 条建议保留、4 条提出问答修订；原稿保存在语义审核候选的 `pre_semantic_review_*` 字段。6 条证据跨多个条款，但仍来自同一文档；其中女职工保护题为七条款长清单，需作为复杂覆盖题单独分析。29/30 个问题写明来源法规，题库尚缺无答案、近似条款和真正跨文档多跳题。所有题目的 `split` 仍为 `unassigned_candidate`，`formal_qa_ready=false`。

产物：[v3 原始候选](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates.json)、[来源映射审核](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/review_packet.md)、[逐题语义审核记录](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/semantic_review_v1.md)、[语义审核候选 JSON](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_semantic_reviewed.json)、[项目 QA 形状](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_project_shape.jsonl)、[候选 manifest](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/qa_candidates_project_manifest.json)。

下一步：见 M2d。已先构造法规名称消融的成对候选，以隔离“问题是否直接写出来源名称”对检索的影响。仍需人工审核 v3 的四项建议修订、成对问法和复杂题分层，并补少量相近条款题；只有冻结语料确有证据链时才增加跨文档多跳题。不可回答/拒答题另作可判定性诊断，不纳入分块策略的主要证据召回排名。完成审核后按文档或证据组划分调参集与保留集，冻结 QA manifest 和哈希，再启动新 B/C 网格。

### M2d：来源名称消融成对 QA（探索性实验版本已固定）

基于 v3 的 30 个事实/证据组，构造 30 对问题、共 60 个问题变体，覆盖 29 份 canonical 文档。每对问题使用相同的问题主体、参考答案、reference context、source spans 和来源文档；仅在带来源名称版本前增加“根据《法规名称》”前缀。它可以测量来源名称这个显式检索线索对当前候选题的影响，但不是 60 个独立事实，也不代表真实用户问法分布或一般的改写鲁棒性。Ragas 生成了原始 QA 候选；这 30 个标题省略版由代理脚本编写，带标题版由脚本加统一前缀生成。若要研究自然问法，应另设题型层，不能把两个实验问题混为一个结论。

生成时执行了来源标题泄漏检查与成对字段一致性设计检查。M2c 已将四项语义修订写入审核版候选；逐对初审中又将女职工题中不自然的“及在工作场所期间”改为“以及劳动场所保护方面”，答案和证据保持不变。其余题的事实与答案证据沿用 M2c 的来源和语义审核；配对生成本身保证法规名称前缀外的主体相同。这是代理审核/结构检查，不是独立人工标注，也不核验外部法律效力。

产物：[配对问题审核包](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/title_ablation_v1/review_packet.md)、[配对候选 JSONL](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/title_ablation_v1/paired_query_candidates.jsonl)、[配对候选 manifest](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/title_ablation_v1/manifest.json)、[来源名称消融生成脚本](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/title_ablation_v1.py)。此外，已生成[探索性实验集 v1](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/ragas_candidate_batch_v3/title_ablation_v1/experiment_set_v1/README.md)，保存 `all.jsonl`、`tuning.jsonl`、`holdout.jsonl` 和哈希 manifest；生成程序为[freeze_paired_qa_experiment_v1.py](/Users/zsm/Documents/DB-GPT/pilot/experiments/qa_generation_2026-10-12/freeze_paired_qa_experiment_v1.py)。按文档分组后，调参集为 23 份文档/23 个事实组/46 个问法，留出集为 6 份文档/7 个事实组/14 个问法（同一 PIPL 文档含两条事实）。所有法规名有/无配对和同文档问题都没有跨组。

**接下来：**先仅用 `tuning.jsonl` 做小规模 B/C 配对诊断，按法规名显式/省略分别报告，并在选定配置前不查看 `holdout.jsonl`。留出集只有 7 个事实组，只能做初步确认；正式结论仍需新增独立文档与人工校验题。近似条款可用于后续错误分析，不是启动这轮分块对照的前置条件，也不应把自动构造的“负例”当成已验证 gold。

### M3：接入最小批量评测闭环

**状态：工程闭环已跑通，代理检查已保存，人工 QA/judge 验收待完成。** 使用四道预选 tuning 正文题 q0007、q0012、q0032、q0034，2 英文/2 中文，涉及三份来源文档；全 14 份语料索引。B/C 均为 512 个 `cl100k_base` 本地 tokens、overlap=51。B 34 块、C 88 块，最终最大长度 502/485，本参数下正文映射无 unresolved。

**实际运行：**两个上下文视角共 16 份回答、64 项 Context Recall/参考答案版 Context Precision/Factual F1/Faithfulness，v2 全部成功；另有 12 项中英文正反控制，正例 1、反例 0。v1 有四个最终指标网络失败和参考答案指代误判，保留诊断；v2 并发 1、最多三次传输重试，补全四题参考答案主语，父 QA/证据不改。两版标准价用量估算合计 0.633129 元，非实际账单。

**已保存产物：**[运行配置 v2](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_config_v2.json)、[运行说明](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M3_RUNBOOK.md)、[runner](/Users/zsm/Documents/DB-GPT/pilot/rag_pipeline/run_model_eval.py)、[完整依赖锁](/Users/zsm/Documents/DB-GPT/pilot/requirements-rag-eval.lock)、[报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/report.md)、[代理逐题检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m3_text_smoke_v2/agent_review.md)，以及完整上下文、原始请求/判断、用量、索引和代码哈希。

**验收边界：**同题关联、失败追溯、参数单位和模型侧用量已跑通，A 未加入。语义评分还会漏掉步骤关系、添加事实含义，参考粒度也影响召回指标；控制样例通过不足以证明业务评分可靠。后续已完成全 24 题代理参考核对与构造 judge 诊断，独立人工验收仍待完成。M3 结果属于试验性链路验收，不选最终策略。

### M4：做 B/C 初始比较与参数粗搜索

**当前状态：准备、全 24 题初始 v4 和网格 v4 已完成。** 以下编号描述原准备产物；当前执行版本和结果以 M4_RUNBOOK 与 `m4_grid_analysis_v4/` 为入口。

1. [24 题核对材料 v3](/Users/zsm/Documents/DB-GPT/pilot/data/eval/qa_tuning_review_packet_v3.md)：补全主语、明确 64 条必答事实与关系，q0010/q0026/q0032 的问题外细节转为可选；保留原答案和父集、ID/split/原文版本。
2. [Judge 控制报告](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/judge_calibration_v1/report.md)：21 控制、42 次按题判断和 32 项官方公式诊断均完成；两套按题提示词各 21/21 与代理预期一致，标准价估算 0.150808 元。严格拆解仍漏关系，不冒充修复或独立人工认证。
3. [候选检查](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_candidates_v1.md)：18 配置、16 独立块集合，本地长度/正文映射通过。C 的 1024 三种重叠完全等价，保留身份并复用计算。
4. [M4 协议](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/M4_PROTOCOL.md)：统一模型/检索/预算和评分；使用 Ragas DiscreteMetric 实现的项目 AnswerAdequacy 为探索性主回答指标，四项官方指标作诊断。按题评分不是 Ragas 内置标准 benchmark，人工状态仍 pending。
5. [初始配置](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_text_initial_config_v1.json) 已 prepare-only；[网格 manifest](/Users/zsm/Documents/DB-GPT/pilot/experiments/chunking/m4_grid_manifest_v1.json) 与 16 个独立运行配置已保存，`run_chunking_grid.py` 提供顺序、缓存恢复入口。尚无这两批模型结果。

**执行工作：**先在全 24 题做 512/51 的 B/C 起始比较，再对全部独立有效候选端到端评测与细化。全量评测策略不按起始点输赢提前淘汰 B/C；如以后改为分阶段筛选，先登记保留协议。

**产物：**候选列表、选择理由、检索与回答指标、运行成本、错误分析。

**验收：**不通过一个参数点提前淘汰某策略；不混用字符/token；不把长度统计当效果结论。

### M5：确认最终配置

**工作：**细化候选、完成必要消融、在保留集确认，记录已知退步与边界。

**产物：**最终建议配置、完整依据、保留集报告、可复现运行说明。

**验收：**明确当前结论覆盖的语料与题型，记录未覆盖代码、多模态和复杂 PDF 表格等场景。

### 紧接着先交付什么

M1/M2/M3及旧M4已有产物；新Markdown候选初筛也已完成。当前交付顺序更新为：

1. 中文29份候选已完成 `zh_enterprise_compliance_canonical_v1` 规范文本与版本/来源记录；它不是原14份 manifest 的原地替换。接下来按问题涉及的法规逐题做官方正文/版本核验。
2. 已在 canonical v1 上实际接入 Ragas `TestsetGenerator` 完成小批候选生成，保存生成配置和原始输出；下一步由人工/项目审核逐题确认问题、答案和 canonical evidence spans。生成器输出不能直接视为 gold QA。
3. 扩充并审核独立于分块结果的新问题、参考答案和证据；覆盖真实条件/例外、相似规则、必要的多证据及不可回答题，排除本轮表格/图片/代码依赖。
4. 冻结新 QA 后，登记 QA 划分、无上下文对照、来源证据检查、模型/检索及 B/C 候选参数，再运行比较。GitLab作为真实企业流程的独立候选，不与中文法规混合成一个总分。

旧网格独立人工审核仍待完成，遗留事项保留：

1. 人工裁决 `m4_grid_analysis_v4/agent_review.md` 列出的 q0009/q0006/q0012/q0017/q0026，保留原始自动标签并标注 judge 漏判/召回漏失。
2. 用新的未参与调参文档/问题做 B/512 候选及 overlap 的确认性评测。
3. 完成分层/逐题差异、RAG 与 judge 成本分开报告，再决定是否细化参数和进入最终验证。

QA 人工状态保持 pending。当前网格是探索性调参结果；M3 512/51 和自动 24/24 不能单独作为最终选型依据。

## 20. 拟议文件组织

下面标注当前实际产物；人工冻结的 `qa_reviewed.jsonl`、正式网格与最终验证仍是后续工作：

```text
/Users/zsm/Documents/DB-GPT/pilot/
├── HANDOFF.md                              已存在，当前交接入口
├── RAG_CHUNKING_EVALUATION_PLAN.md          本详细方案
├── .venv-rag-eval/                         已创建：Python 3.11 独立评测环境
├── requirements-rag-eval.in               已创建：直接依赖
├── requirements-rag-eval.lock             已创建：完整固定依赖
├── data/
│   ├── rag_corpus/                         已有语料与历史清单
│   └── eval/                               已创建语料与 QA 代理初审产物
│       ├── corpus_manifest.json            已创建：首轮暂定冻结语料
│       ├── _corpus_review.md               已创建：文本层审核报告
│       ├── qa_seed.jsonl                   已创建：依据原文行范围的 QA 候选
│       ├── qa_provisional.jsonl            已创建：代理初审、offset 和 Ragas 对应字段
│       ├── ragas_input_provisional.jsonl   已创建：Ragas-compatible 字段转换，未评分
│       ├── qa_review_log.md                已创建：逐题人工审核占位
│       ├── qa_splits.json                  已创建：按文档划分
│       ├── qa_quality_report.md            已创建：覆盖、审核边界、限制
│       ├── qa_review_decisions_v2.json      已创建：逐题回源判断与修订输入
│       ├── qa_seed_v2.jsonl                 已创建：修订后的行范围与问题
│       ├── qa_source_reviewed_v2.jsonl      已创建：44 条代理复核 QA
│       ├── qa_text_only_v2.jsonl            已创建：首轮 35 道正文题，调参 24/原保留 11
│       ├── qa_text_only_v2_manifest.json    已创建：范围决定、选择规则、题目 ID 与父/子集哈希
│       ├── qa_audit_v2.jsonl                已创建：逐题修改、旧题及理由
│       ├── qa_review_packet_v2.md           已创建：可直接阅读的逐题原文材料
│       ├── qa_quality_report_v2.md          已创建：复核结果与边界
│       ├── qa_version_manifest_v2.json      已创建：输入/输出/脚本哈希
│       ├── ragas_input_v2.jsonl             已创建：字段转换，未评分
│       └── qa_reviewed.jsonl               待做：人工审核后冻结
├── rag_pipeline/
│   ├── corpus_baseline.py                  已存在
│   ├── corpus_clean.py                     已存在
│   ├── chunkers.py                         已存在，后续适配 B/C
│   ├── word_parser.py                      已存在
│   ├── prepare_eval_dataset.py             已实现：QA 解析和哈希校验
│   ├── review_eval_dataset.py              已实现：按显式决策重建复核版与报告
│   ├── evaluate_chunking.py                已实现：离线 TF-IDF 诊断
│   ├── ragas_adapter.py                    已实现：Ragas 字段转换，未评分
│   └── run_model_eval.py                   已实现并运行：M3 embedding/回答/Ragas
└── experiments/chunking/                   已有离线诊断及模型评测
    ├── M3_RUNBOOK.md                       已创建：复现、设置、版本与限制
    ├── m3_text_smoke_config_v1.json        已创建：历史诊断配置
    ├── m3_text_smoke_config_v2.json        已创建：当前四题试跑配置
    ├── m3_api_cache_v1/                    已创建：按请求/向量哈希缓存
    ├── m3_text_smoke_v1/                   已保存：指代误判与网络失败诊断
    └── m3_text_smoke_v2/                   已保存：16 回答、64 指标，全部成功
        ├── config.resolved.json            完整设置、数据/代码/锁文件哈希
        ├── environment.json                实际解释器与安装版本
        ├── selected_qa.json                原 QA，人工状态不变
        ├── preparation.json                块长、块数与来源映射检查
        ├── *_chunks.json / *_vectors.npy   两策略块记录及独立向量索引
        ├── results.json                    排名、实际上下文、回答、评分
        ├── api_events.jsonl                请求/返回、用量、错误与缓存事件
        ├── judge_controls.json             中英文正反控制
        ├── summary.json / attempt_usage.json 汇总与本次执行用量
        ├── report.md / *_fixed_*.md         逐题报告及完整上下文
        └── agent_review.md / agent_review_evidence.json 代理检查与原判断摘录
```

实际接入后可以调整文件名和格式，但需保留职责边界与可追溯关联，不把候选、审核数据、运行结果混成一个随时修改的文件。

本轮新增产物（均已保存；模型初始比较和网格结果尚未生成）：

- `data/eval/qa_text_tuning_v3.jsonl`、对应 manifest、`qa_tuning_review_decisions_v3.json` 和 `qa_tuning_review_packet_v3.md`：24 题参考核对与必答事实。
- `data/eval/judge_calibration_v1.jsonl`、对应 manifest 与 `experiments/chunking/judge_calibration_v1/`：21 例控制及真实评分结果。
- `rag_pipeline/prepare_tuning_review.py`、`prepare_judge_calibration.py`、`run_judge_calibration.py`、`calibrated_metrics.py`：数据准备与项目按题评分。
- `experiments/chunking/M4_PROTOCOL.md`、`m4_candidates_v1.json/.md`、`m4_text_initial_config_v1.json`、`m4_grid_manifest_v1.json` 和 `m4_grid_configs_v1/`：冻结规则、候选与可执行配置。
- `rag_pipeline/prepare_chunking_candidates.py`、`prepare_grid_configs.py`、`run_chunking_grid.py`：候选准备、配置生成和顺序可恢复执行。

## 21. 历史记录的保留与纠正

### 21.1 历史块形态统计

以下来自旧交接记录，使用全量 195 份含大量代码块的语料及 `chunk_size=800`、`overlap=100`、代码保护开启；本次文档更新没有重新运行产生这些数据。

| 策略 | 块数 | 平均字符长度 | 中位长度 | 小于 100 字符 | 围栏不平衡率 |
|---|---:|---:|---:|---:|---:|
| A 固定长度 | 678 | 1,337 | 800 | 0 | 0.6% |
| B 递归字符 | 737 | 1,231 | 778 | 9 | 0.5% |
| C 结构感知 | 1,555 | 560 | 299 | 118 | 0.3% |

围栏不平衡只是代码块切断的代理检查，不是完整代码结构验证。标题注入和代码占位符还原可能使实际块长超过名义配置。

旧 B 代码保护消融记录：关闭时 1,412 块、含代码块的 chunk 730 个、围栏不平衡率约 42.2%；开启时 737 块、含代码 chunk 387 个、不平衡率约 0.5%。

这些记录解释了代码保护的历史动机，但第一阶段暂不研究代码，不把它们作为 B/C 检索或回答效果证据。部分探索脚本位于临时目录，尚未纳入完整可复现实验产物。

### 21.2 已纠正的旧说法

| 旧记录 | 当前修正 |
|---|---|
| 无代码/无表格只剩 19 份 | 按已有 manifest 为 21 份；再加 7 份无代码表格文档共 28 份候选 |
| 62 份表格文档都可加入无代码实验 | 其中 55 份同时有代码，不能全部加入 |
| 中文目录中的 28 份可视作真正中文 | 旧统计口径不一致；目录不代表语言，需按冻结数据重新审核 |
| 排除代码语料这条路不可行 | 第一阶段可用 28 份候选建立流程；正式代表性需另评估 |
| 16 份 Word 无法做统计实验 | 文档数量不是决定性标准，需看独立题目与内容覆盖 |
| PDF 未解析 | 已转换全部 21 份，主要问题转为结构和表格质量 |
| 实验固定为 A/B/C | 用户已要求退出 A，后续比较 B/C |
| 只缺 QA，其他都就绪 | 此前还缺模型 runner 等；现已跑通 M3，但正式搜索、人工 QA/judge 校准及最终验证仍待完成 |
| 参考答案必须是原文连续片段 | 答案可以准确归纳，证据需真实充分并可回源 |
| 禁止跨文档题 | 可按实际需求设计多来源证据，第一轮可控制难度 |
| 前 60 字符匹配足以做公平命中 | 不再作为主评测，使用语义支持指标及可选可靠区间评估 |
| 400/800/1200/1600 与 0/10/20% 是标准候选 | 不存在该统一标准；当前本地 token 粗搜清单是有理由和版本的项目实验设计 |
| 结构感知是本项目独有方案 | 是公开常见技术组合；价值在适配、评测和实证选择 |

## 22. 来源与结论的对应关系

### 22.1 证据等级

本文使用四种类型：

- **公开来源事实：**官方文档、源码或研究报告直接支持。
- **本地观察：**已有文件、代码、manifest、解析报告或只读统计支持。
- **项目建议：**为本项目作出的工程选择，尚需实施或验证。
- **未确定事项：**尚未选择、审核、实现或运行。

### 22.2 关键主张的来源

| 主张 | 来源 | 不能延伸成什么 |
|---|---|---|
| 固定问题、比较参数配置与索引结果 | LlamaIndex 参数优化；AutoRAG 流程 | 所有企业均采用同一个实验框架 |
| 使用问题、参考答案、上下文、回答进行 RAG 评测 | Ragas schema 和指标文档 | Ragas 自动保证题目正确或自动选最优分块 |
| 应记录支持答案的实际原文与页码 | Azure 准备阶段 | Azure 定义了本文字符覆盖公式 |
| 可通过证据片段来源区间比较分块召回 | Chroma 研究及源码 | 当前弱 `locate_spans` 已满足精确评估要求 |
| 512 tokens、一定比例重叠可作起点 | Azure 分块建议 | 512 中文字符最佳、25% 必须采用 |
| 当前无代码候选为 28 份 | 本地 manifest 与清洗文件 | 28 份均已审核合格、代表中文企业知识库 |
| 仅比较 B/C，暂不考虑代码块 | 用户决策 | 代码中的 A 函数已删除 |
| 同时控制 token 预算、按事实簇划分数据 | 本项目公平性与防泄漏设计建议 | 任一框架强制如此、已有实验验证收益 |

### 22.3 主要官方资料入口

1. [Ragas 官方仓库](https://github.com/vibrantlabsai/ragas)。
2. [Ragas SingleTurnSample](https://docs.ragas.io/en/stable/references/evaluation_schema/#singleturnsample)。
3. [Ragas 测试集生成](https://docs.ragas.io/en/latest/getstarted/rag_testset_generation/)。
4. [Ragas 测试集语言适配](https://docs.ragas.io/en/latest/howtos/customizations/testgenerator/_language_adaptation/)。
5. [Ragas 实验模板](https://docs.ragas.io/en/stable/howtos/cli/rag_eval/)。
6. [Ragas Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)。
7. [Ragas Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)。
8. [Ragas Factual Correctness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/)。
9. [Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)。
10. [Ragas 评审模型对齐](https://docs.ragas.io/en/stable/howtos/applications/align-llm-as-judge/)。
11. [RAGAS 原始论文，EACL 2024](https://aclanthology.org/2024.eacl-demo.16/)。论文与当前 SDK 能力需按版本区分。
12. [LlamaIndex 参数优化示例](https://developers.llamaindex.ai/python/examples/param_optimizer/param_optimizer/)。示例标注实验性质，接入时需确认当前 API。
13. [LlamaIndex 块大小评估案例](https://www.llamaindex.ai/blog/evaluating-the-ideal-chunk-size-for-a-rag-system-using-llamaindex-6207e5d3fec5)。单案例结果不等于普遍最优。
14. [Azure RAG 准备阶段](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-preparation-phase)。
15. [Azure 文档分块](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents)。
16. [Azure 检索阶段评估](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-information-retrieval#evaluate-your-search-results)。
17. [Azure 回答阶段评估](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-llm-evaluation-phase)。
18. [Azure RAG Experiment Accelerator](https://github.com/microsoft/rag-experiment-accelerator)。
19. [AutoRAG 仓库](https://github.com/Marker-Inc-Korea/AutoRAG)、[legacy README](https://github.com/Marker-Inc-Korea/AutoRAG/blob/main/legacy/README.md)。
20. [AutoRAG 分块数据](https://marker-inc-korea.github.io/AutoRAG/data_creation/chunk/chunk.html)、[QA 映射](https://marker-inc-korea.github.io/AutoRAG/data_creation/tutorial.html)、[优化流程](https://marker-inc-korea.github.io/AutoRAG/tutorial.html)。
21. [Chroma 分块评测报告](https://www.trychroma.com/research/evaluating-chunking)、[开源评测](https://github.com/brandonstarxel/chunking_evaluation)。
22. [DeepEval RAG 入门](https://deepeval.com/docs/getting-started-rag)、[开源仓库](https://github.com/confident-ai/deepeval)。
23. [Optuna](https://github.com/optuna/optuna)。
24. [RagBuilder 当前维护说明](https://github.com/KruxAI/ragbuilder)。

外部框架版本与维护状态是本次调研记录，正式接入前应再确认。涉及 SDK 的实现应固定版本，不直接把网页上的 latest 示例与本地旧环境混用。

## 附录 A：28 份无围栏代码块 Markdown 候选清单

根目录：`/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs/cleaned/`。

下表计数为当前清洗文件的非空白字符数；类别沿用原 manifest 的代码/表格识别口径。表内文件尚未全部完成正式语料质量审核。

### A.1 无代码块、无表格：21 份

| 相对路径 | 非空白字符数 |
|---|---:|
| `modules/smmf.md` | 10,573 |
| `changelog/Released_V0.6.0.md` | 4,810 |
| `application/apps/chat_knowledge.md` | 2,586 |
| `application/awel.md` | 1,961 |
| `application/advanced_tutorial/smmf.md` | 2,434 |
| `application/apps/app_manage.md` | 1,826 |
| `agents/modules/action/action.md` | 2,294 |
| `application/prompts.md` | 1,379 |
| `application/apps/chat_db.md` | 811 |
| `dbgpts/how-to-use-custom-skill.md` | 1,157 |
| `application/apps/chat_data.md` | 786 |
| `awel/why_use_awel.md` | 912 |
| `dbgpts/how-to-use-skill.md` | 875 |
| `agents/modules/resource/resource.md` | 847 |
| `application/apps/chat_excel.md` | 514 |
| `agents/modules/resource/database.md` | 811 |
| `application/apps/app_chat.md` | 657 |
| `application/datasources.md` | 531 |
| `application/apps/app_explore.md` | 527 |
| `agents/modules/resource/knowledge.md` | 601 |
| `tutorials/index.md` | 215 |
| 合计 | 37,107 |

### A.2 无代码块、有表格：7 份

| 相对路径 | 非空白字符数 |
|---|---:|
| `agents/introduction/tools.md` | 2,286 |
| `modules/connections.md` | 2,148 |
| `getting-started/next-steps.md` | 2,006 |
| `agents/modules/resource/tools.md` | 1,391 |
| `installation/index.md` | 899 |
| `application/graph_rag.md` | 954 |
| `installation/integrations.md` | 958 |
| 合计 | 10,642 |

## 附录 B：恢复工作时的最小阅读顺序

1. [HANDOFF.md](/Users/zsm/Documents/DB-GPT/pilot/HANDOFF.md)：当前状态、已确认决策、下一步。
2. 本文第 4–6 节：语料边界与分块实现限制。
3. 本文第 8–12 节：QA、数据模型及指标的准确含义。
4. 本文第 13–19 节：公平比较、参数搜索与实施里程碑。
5. 根据需要回看来源链接和原始解析报告。

接手后先读 HANDOFF 和 M4_PROTOCOL，核对 v3 调参 manifest、21 例控制报告及候选检查；再回看 M3。下一步实际运行全 24 题初始比较和已准备的网格。参考代理核对、依赖/来源映射及最小闭环均有产物，人工验收仍 pending；不要重复列为尚未开始，也不要恢复历史 195 份含代码语料、A 或 60 字符命中法。

## 附录 C：尚待确定的具体选择

| 选择 | 影响 | 建议处理时机 |
|---|---|---|
| 正式目标是技术知识库还是中文企业制度/SOP | 语料、题型、语言分布与结论范围 | 语料冻结前明确；试跑可先用现有候选 |
| 是否补充审核后的中文 Word | 正文/表格代表性、解析变量 | M1 的后续扩展或新 corpus version |
| 是否需要真实用户查询样本 | QA 真实性、业务覆盖 | M2 |
| QA 题型、无答案题及多文档题范围 | 指标与标注规则 | M2 |
| M4 的计量口径 | v1 已沿用 tiktoken 0.9.0 / cl100k_base；18 个候选的最终块长均已检查，本地计数不同于 provider | 新候选/新 corpus 时重查 |
| M4 模型与 judge | v1 沿用已接入模型，21 例代理控制完成；同模型自评偏差和独立人工一致性仍需核对 | 探索性运行后、最终确认前 |
| Ragas 版本及指标 | v1 固定 0.4.3 四项官方诊断，加项目 AnswerAdequacy criteria_v1；不宣称官方关系误判已修复 | 新评分版本另存、重新比较 |
| top-k 与共同预算 | v1 固定 top-k=5、前 20 块按排名装入 2560 本地 token 上限 | 全部候选一致执行 |
| 标题注入 | v1 固定 C 注入标题，计入块长；不作为首轮额外变量，表格专项已暂缓 | 后续消融另存版本 |
| 正式验收阈值与质量/成本取舍 | v1 已登记探索性 Pareto 与逐题比较规则；业务门槛仍未提供 | 最终上线建议前 |
| 精确证据覆盖 | 当前仅作主要证据区间定位诊断，不替代语义质量评分 | 新标题/替代证据映射另审 |

M4 v1 的参考、控制诊断、粗搜候选、模型/预算和探索性比较协议已有产物；当前应运行全 24 题初始比较和全部独立候选，人工审核仍待完成。最终策略/参数由实际 M4/M5 实验决定；候选准备、构造控制和四题试跑都不能代替选型。
