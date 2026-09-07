# P0 Execution 迁移说明

## 新增入口

```powershell
workspace execution create REQ-021 --task TASK-001 --runtime codex --prompt "实现功能"
workspace execution list REQ-021
workspace execution show EXE-000001
workspace execution events EXE-000001
workspace execution migrate-legacy REQ-021
workspace execution demo REQ-021
```

`execution demo` 使用确定性 fake Runtime，但经过与正式 Runtime 相同的 `RuntimeExecutor.execute_tracked()` 路径，可用于无外部 Agent、无网络环境的快速验证。

## 数据位置

- Execution：`.workspace/REQ-xxx/executions/EXE-xxxxxx.json`
- Runtime 事件：`.workspace/runtime-events/*.jsonl`

## 兼容边界

- 不删除或改写原 `sessions.json`。
- `migrate-legacy` 可重复执行；相同 Session 不重复创建 Execution。
- 多 Task 旧 Session 会按 Task 拆成多个 legacy Execution，并保留完整原 Session payload。
- `execution events` 使用统一的 `after/limit` 查询语义，不会因单个 Execution 超过 1000 条而静默截断事实。
- delegate 在 Task 发布为 `in_progress` 前持久化 queued Execution；响应丢失会读取 Provider 状态核对，活动生命周期内的相同请求重试复用原 Execution，终态后允许同内容新委派。
- V1 测试替身和外部旧执行端口暂时保留非 tracked 回退；产品正式组装已配置 ExecutionStore。
- P0 不改变 Hook 行为，也不实现 Main Agent、Supervisor 或多 Agent。
