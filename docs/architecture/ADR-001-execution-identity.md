# ADR-001：Execution 使用独立、项目级身份

## 状态

P0 已采用，后续阶段继续验证。

## 决策

- `Task != Execution != Agent Session`。
- Execution ID 在项目内全局唯一，并在工作区全局锁内分配。
- 新 Runtime 调用以 `execution_id` 作为 `run_id`，事件同时保留 `requirement_id`、`task_id`、`execution_id`、`runtime_id` 与 `session_id`。
- Runtime Session 只是 Execution 的可恢复引用，不能作为 Execution 主键。
- 核心数据保持人类可读 JSON；Runtime 原始事件继续使用可重放 JSONL。
- delegate 的内容指纹只在对应 Task/Execution 仍未决或运行中时用于重试幂等；终态后相同内容可以创建新的合法 Execution。

## 原因

一个 Task 可以重试、更换 Provider 或增加 reviewer Execution；一个 Session 也可能只承载某次 Execution。把三者绑定成一对一会破坏历史追踪和后续路由、Supervisor、多 Agent 能力。

## 兼容性

旧 `sessions.json` 不修改；一个旧 Session 的每个 `task_ids` 分别映射为共享原 Session 引用的 legacy Execution，并以 Session 自带 `agent` 作为 Runtime 身份。现有不支持追踪扩展的 V1 `AgentExecutionPort` 仍可运行，但正式组装的 RuntimeExecutor 始终走 tracked 路径。
