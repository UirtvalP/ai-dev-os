# ADR-006：Main Agent 推荐与 Execution Policy 裁决分离

## 决策

Main Agent 只产生版本化 `ExecutionRecommendation`，表达 Provider/Runtime、Model、
Reasoning、Role 与 Parallelism 建议。`ExecutionRoutingPolicy` 使用 Runtime 实时报告的能力与模型，
结合 Task 硬约束、sandbox、并行上限和 V1 单写者约束，产生最终 `ExecutionPolicyResult` 与可审计
`PolicyDecision`。

建议是软输入：不可用的建议会被明确拒绝并安全回退；Task 已声明的显式偏好是硬约束，不能静默忽略。
Policy 不包含任何具体 Provider 或 Model 名称，Router 与 Policy 均通过端口可替换。

## 原因

这使 Main Agent 可以因 Requirement 上下文提出不同执行角色和资源建议，同时避免它绕过真实能力、
权限隔离或系统并行限制。Provider/Model 列表来自 Runtime discovery，不随产品代码固化。

## 边界

本阶段只裁决并行度，不启动多个 Execution。V1 写任务仍限制为单写者；真正的多 Agent 生命周期、
隔离和汇合由 P9 实现。
