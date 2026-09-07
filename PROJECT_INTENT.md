# 项目意图

## 目的

摘要：AI Dev OS 让用户的目标、推理、进度和下一步行动能够跨越可替换的 Agent 会话恢复。

系统应让新 Thread（线程）以尽量少的重复分析继续推进 Requirement（需求），同时保留人类可读、本地优先的状态。

## 期望结果

摘要：恢复后的 Agent 应知道要做什么、为什么选择当前方向、已经完成什么以及下一步做什么。

dashi 中由用户明确移到 `in_progress` 的开发 Task 应自动启动或恢复 Codex 执行，不要求用户再手工创建 Thread 或重复发送继续指令。

## 不得演变成

摘要：V1 不得演变成重量级自主 Agent 平台、工作流官僚体系，也不得取代现有的编码、任务和 Git 工具。

不得添加数据库、Web UI、多 Agent 调度器、推测性的集成框架或自动知识写入系统。

上述限制是 V1 compatibility profile 的边界，不是项目永远停止演进的限制。`REQ-020` 经用户明确授权开启 V2：可以增量增加编辑器无关 Agent Runtime、确定性多 Agent 编排、Git Integration/Merge Gate、Verification Provider、Dashboard/Event Plane 和 main-only Deployment Gate，但必须保留 V1 入口与持久状态，不得把 V1 推倒重写，也不得演变成与软件交付无关的通用分布式调度平台。

V2 的新增能力必须满足：

1. Requirement Workspace 继续是目标、意图、验收、决策和交接的 Source of Truth。
2. 调度、租约、质量门禁、合并和部署等确定性状态由本地 Orchestrator 权威持久化；Task Provider 是可替换投影与用户输入边界。
3. Conversation/Event 是可观测执行数据，不取代 Requirement State。
4. Worker 与 Primary Agent 只能声明候选完成；只有系统 Gate 能完成 Requirement。
5. Dashboard 只是 Workspace 与执行事件的控制面，不成为第二套事实来源。
6. 部署只能接受受保护 `main` 的已验证提交。

## 取舍优先级

摘要：首先保留意图和可恢复性，其次是简单性与实用价值，最后才是可扩展性和架构优雅性。

发生冲突时，按以下顺序取舍：

1. 正确理解用户意图和需求意图
2. 可靠的恢复、检查点、交接和上下文快照
3. 最低复杂度和人类可读的本地状态
4. 在真实外部边界保持 Provider（提供方）无关性
5. 未来可扩展性

V2 冲突取舍仍遵守以上顺序；“多 Agent”或“可插拔”本身不能成为增加抽象的理由，只有当前阶段验收与已证明风险需要时才实现。

## 执行优先级

摘要：确定性代码和规则自动化优先于任何 AI 语义判断或完整 Agent 推理。

1. Deterministic code
2. Rule-based automation
3. Small semantic decision
4. Full Agent reasoning

Session、项目与 Git 发现，唯一匹配选择，绑定、同步、结构化状态收集、已知验证命令、checkpoint 和 handoff 等可确定流程属于 Automation Layer。Automation Layer 不调用 LLM、不依赖对话历史、尽量幂等且必须可测试。
