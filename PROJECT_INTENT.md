# 项目意图

## 目的

摘要：AI Dev OS 让用户的目标、推理、进度和下一步行动能够跨越可替换的 Agent 会话恢复。

系统应让新 Thread（线程）以尽量少的重复分析继续推进 Requirement（需求），同时保留人类可读、本地优先的状态。

## 期望结果

摘要：恢复后的 Agent 应知道要做什么、为什么选择当前方向、已经完成什么以及下一步做什么。

## 不得演变成

摘要：V1 不得演变成重量级自主 Agent 平台、工作流官僚体系，也不得取代现有的编码、任务和 Git 工具。

不得添加数据库、Web UI、多 Agent 调度器、推测性的集成框架或自动知识写入系统。

## 取舍优先级

摘要：首先保留意图和可恢复性，其次是简单性与实用价值，最后才是可扩展性和架构优雅性。

发生冲突时，按以下顺序取舍：

1. 正确理解用户意图和需求意图
2. 可靠的恢复、检查点、交接和上下文快照
3. 最低复杂度和人类可读的本地状态
4. 在真实外部边界保持 Provider（提供方）无关性
5. 未来可扩展性

## 执行优先级

摘要：确定性代码和规则自动化优先于任何 AI 语义判断或完整 Agent 推理。

1. Deterministic code
2. Rule-based automation
3. Small semantic decision
4. Full Agent reasoning

Session、项目与 Git 发现，唯一匹配选择，绑定、同步、结构化状态收集、已知验证命令、checkpoint 和 handoff 等可确定流程属于 Automation Layer。Automation Layer 不调用 LLM、不依赖对话历史、尽量幂等且必须可测试。
