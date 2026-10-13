# LongMemEval-S 接线清单

## 规划

- [x] 固定数据、隔离和指标口径
- [x] 确认只做薄适配，不改记忆规则
- [x] 写明 API 失败和官方评分限制

## 实现

- [x] 解析官方 LongMemEval JSON
- [x] 按时间顺序回放 history
- [x] 为每题创建独立 scope/store
- [x] 记录 retrieval candidates、source refs 和答案
- [x] 输出官方 scorer 可读 JSONL
- [x] 增加无模型 smoke
- [x] 用 mini fixture 完成 1 题 API 端到端 smoke

## Pilot

- [x] 执行无模型 smoke
- [x] 检查问题没有写入记忆（mini smoke）
- [x] 检查 scope 没有串题（每题独立数据目录）
- [x] 下载官方 cleaned 数据并执行 5 题 API pilot
- [x] 追加不同 question type 的 5 题诊断样本
- [x] 单独运行 1 道 `_abs` 弃答题并人工核对已知信息与拒答边界
- [x] 保存各次运行的 manifest、predictions 和逐题记录
- [x] 用 DashScope `qwen3.8-max` 对 11 道诊断样本运行自定义 judge，并保存逐题标签、指标和运行 manifest
- [ ] 如需与论文分数对比，另用 LongMemEval 固定版本的 GPT-4o judge 与官方汇总脚本复跑；DashScope 结果不等同于官方/论文口径

## 关闭

- [x] 运行结果可重放（数据集 SHA、配置、版本和输出均已记录）
- [x] 记录 DashScope 自定义 judge 的总体/分类/弃答指标及分母（11 题小样本，非官方分数）
- [ ] 记录 GPT-4o 官方协议总体/分类/弃答指标及分母
- [ ] 决定是否扩大到 20–30 题或完整 S
