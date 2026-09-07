# P9 一等 Multi-Agent 迁移说明

## 运行路径

既有命令保持不变：

```powershell
ai-dev-os orchestration prepare REQ-ID --file request.json --expected-main <SHA>
ai-dev-os orchestration plan REQ-ID --file prepared.json --owner <OWNER> --max-workers 2
ai-dev-os orchestration run REQ-ID --owner <OWNER> --max-workers 2
ai-dev-os orchestration verify REQ-ID --owner <OWNER>
ai-dev-os integration merge REQ-ID --request-id <ID> --expected-main <SHA>
```

变化在 Composition Root：真实 `RuntimeWorkerPort` 由 `ExecutionTrackedWorkerPort` 包装。每个 attempt 会产生 `source=supervisor` 的 Execution；Integration Gate 可由 `IntegrationExecutionService` 记录为 `source=integration` 的 Execution。

## 并行规则

- 依赖 Task 必须 accepted 后才可启动。
- `max_workers` 是硬上限。
- 两个 read-only Task 可共享显式相同工作区。
- 任一 write-capable Task 都必须使用独立 worktree/branch；重叠目录 fail closed。
- candidate/verifying/unknown 状态继续保留目录所有权。

## 中断恢复

- Worker 的 TaskSpec、Route、attempt/fence、branch/worktree 与 Session 必须精确匹配；观察或清理结果未知时 Execution 转为 `waiting`，不得重启第二个 Worker。
- Integration 失败或在 Gate 返回后崩溃时，用同一 `request_id` 重试 `integration merge` 或执行 `integration reconcile`；既有 Gate 负责幂等恢复，返回 receipt 后回填原 Integration Execution。

## 回滚

回滚 P9 提交会恢复原 Worker Port 直连；新增 Execution 与中央 raw events 可保留为历史事实，不影响原 Worker ledger、Task、候选或 Integration journal。
