# V2 编排与策略控制面

当前是 REQ-020 Phase 2 实现候选，尚未取得本阶段 Gate。Phase 0、Phase 1 已验收；后续 Git Integration、Verification Provider、Dashboard、Deployment 不因本文件存在而视为完成。

## 使用边界

Requirement Workspace 继续保存目标、意图、验收和阶段事实。新增的每需求 `orchestration/supervisor/state.json` 只保存执行计划、调度与候选；Task Provider 是独立投影，Agent 对话是事件数据。V1 Dispatcher、Hook-first 与 Review Gate 入口继续保留，不重复 bootstrap 或认领原 Task。

默认规则 Planner 对一个结构化 Task 输出 direct，对多个 Task 输出 DAG；不会冒充自然语言智能拆解。替换 PlanningPolicy 可以生成新节点，Supervisor 仍检查授权根、节点数、依赖、重试预算和权限上限。ModelRouter、RecoveryPolicy、VerificationPlanner 使用同样的版本化端口，可替换但不能获得 Gate 写权限。

授权按原 Task 保留，不能借用其他节点的写权限、重试额度或模型偏好。多源拆解必须以 `source_task_ids` 明确来源；组合权限取交集、预算取最小值，冲突的显式偏好拒绝执行。原 Task 的空偏好继续代表自身默认选择，不被另一个节点的偏好覆盖；来源与路径绑定进入计划和尝试历史，重规划也不能改变授权来源。

Worker 只有 running/unknown 运行观察与 candidate_complete/blocked/failed 终态。candidate_complete 要在整个进程树已停止后，由可信候选读取器给出 SHA/tree；Worker 文本、退出码和自报 PASS 不具备授权。Verification Executor 缺失时不验收；所有节点 accepted 也只进入 ready_for_integration，不能完成 Requirement 或部署。

## 本地命令

以下命令针对已经存在的 Requirement，控制面与各 Task 目录必须分离：

```text
ai-dev-os orchestration plan REQ-123 --root C:/projects/product --owner operator-unique --file plan.json
ai-dev-os orchestration status REQ-123 --root C:/projects/product
ai-dev-os orchestration run REQ-123 --root C:/projects/product --owner operator-unique --timeout 300
```

`plan.json` 是版本化 PlanningRequest，不是可执行 Python 或工作流 DSL：

```json
{
  "schema_version": 1,
  "requirement_id": "REQ-123",
  "goal": "实现一个已定义且可验收的功能",
  "tasks": [{
    "schema_version": 1,
    "task_id": "TASK-A",
    "title": "完成独立模块",
    "prompt": "按本任务的验收约束实现；只提交候选结果。",
    "complexity": "normal",
    "write_required": true,
    "worktree": "C:/task-worktrees/TASK-A",
    "branch": "feat/TASK-A",
    "retry_budget": 1
  }]
}
```

初始精确 Task 目录默认就是规划授权边界；专业 Planner 需要创建额外节点时，操作员可显式传 `--allow-worktree-root`。它不自动创建或分配 Git worktree；真实分支身份、候选提交和合并证据由后续 Git 边界提供，不能仅凭 JSON 的 branch 字段证明。

## 运行、恢复和投影

- Supervisor 独占带 fence 的有限租约；每次调度先原子持久化 attempt 意图。相同尝试 ID 不重放，失效租约不能写入或发新 Worker。
- 真实启动持有账本与 Supervisor 授权锁，在创建进程前及恢复挂起线程前再检查期限/epoch；延迟的旧线程不能跨过已生效的新 epoch 启动。关闭请求与清理完成是两种状态，清理失败保留 unknown 和槽位，重复 close 必须重试真实清理。
- 重启时没有旧进程句柄就保留 unknown 及并发槽位。只有可信 launcher 确认旧进程树取消后才允许按预算重试，不通过 PID 猜测或“日志为空”判断可重放。
- Worker 端口与事件日志都在控制面。事件入口只接收当前 run/runtime 的版本化事件；它不是任意路径写入或 Authority API。
- TaskProjection 使用独立账本及明确的本地 Task ID → 既有 Provider Task ID 映射。无映射不会创建卡片。候选与 accepted 最多映射为 in_review，永不写 done。
- 映射卡片在 V2 初始化/发送前先写入 canonical `.workspace/execution-ownership/state.json`；与 V1 Dispatcher 认领共用 Workspace 锁。卡片 ID/原始 ID 都受保护，V1 在候选、认领与启动边界检查；没有持久认领不能投影 in_progress。认领不随服务退出、重启或 Provider 离线消失，本阶段不自动解除认领。
- Provider 离线保留最后状态和写意图；恢复用版本 CAS 收敛。人工变更不抢回。由于 V1 Provider 没有操作幂等 ID，响应丢失后只能确认状态对齐，不能伪造写者归属；不重复 CAS，并保留需要重新确认所有权的状态。

要投影既有卡片，在初始 PlanningRequest 的顶层加入 `"task_provider_bindings": {"TASK-A": "AID-123"}`。前台服务通过单线程后台投影泵提交最新节点快照，Provider I/O 不阻塞 Supervisor 续租；没有明确映射就不发起外部写入。结束时有限等待，离线未完成的 pending 留给下一次恢复，而不是假报全部同步成功。

首次建立卡片认领需读取在线 Provider 并排除旧 Dispatcher/活动 Session，已有认领的恢复与调度不依赖在线 Provider。前台结束时先提交取消后的实际 stopped/unknown 快照，再有限等待投影泵；离线账本记录最终目标，不保留过时 running 目标。

## 权限与能力诚实

Provider 的 read-only/workspace-write 是运行模式，不是整个 Worker 的 OS 权限证明。当前受信装配采用 Windows LPAC 外层隔离，进程创建时即受限且挂起，加入不可脱离的 Job 后才能执行；关闭需确认 Job 已无活动进程。必要能力通过新建临时域中的真实恶意进程验证，不攻击用户的真实 Gate、Workspace 或策略文件。

Worker 的环境按白名单重建；不会继承控制面会话 ID、连接 token、代理或用户 PATH。缓存、临时文件及需要复制的工具位于自己的 Task 私有目录。不对用户安装目录、系统账户、虚拟机或全局配置扩大权限。控制面自身必须在 Task 写域外；可写 editable 安装不能把自己的源码目录作为 Worker Task。

Task 权限只逐对象增删本次随机 SID 的 ACE，保留既有 ACE 与继承标志。这里有意使用不自动向既有子对象传播的 [SetFileSecurityW](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-setfilesecurityw)，逐一更新已校验的物理路径；不使用 icacls 自动继承重算，也不以旧 ACL 整包回滚覆盖后续权限修改。

目前的 read-only 模式不额外保证 Task 域内部完全不可写；保证的是不能写控制面和其他 Task。更严格的只读源码/可写 scratch 划分需要后续明确能力支持，不能用模式名宣称已经实现。

默认禁用网络；`--allow-network` 是显式的外网能力授权，不是域名过滤。未证明本机后端、工具读取或认证可用时返回不可用，绝不退回普通进程。现有 Codex 的 Phase 1 在线 smoke 不代表已经通过 LPAC 下的在线认证；Cursor/Claude 本机未安装也不能用 fake 协议测试冒充 live PASS。

Codex 的模型与思考强度来自实际 model/list；路由选择的 effort 向每轮 turn/start 原样传递。未协商 effort 控制的 Adapter 对显式请求返回 unsupported，不悄悄忽略。[官方 App Server 协议](https://learn.chatgpt.com/docs/app-server)

Cursor/Claude 的首次 describe 使用独立临时目录和短连接进行协议模型发现，不发送用户 prompt。实际运行重新协商，不把发现缓存当作本次会话证据；发现失败或进程清理未确认时不发布可用模型。协议 fixture 验证了标准装配的 describe → route → start 接线，不能替代未安装产品的本机 live 验证。

## 验收节奏

遵循 DEC-008：阶段内先写完与集成，仅保留必要接口和安全快速检查；再集中跑完整自动测试/回归与一次独立 Review，发现的问题批量修复并最终复验。阶段 Gate 未通过不得进入下一阶段；不把六个阶段的验收全部延后。
