## 8. Phase 2 — Orchestration / Policy Plane（AID-171）

### 实现范围

1. `PlanningPolicy` 输出 `ExecutionPlan(mode=direct|dag)`；Core 不解析自然语言拆解任务。
2. `ModelRouterProvider` 根据 Task 特征和 Runtime capability 选择 Runtime、model、effort、权限配置。
3. `VerificationPlannerProvider` 先生成可审计测试意图；Phase 4 再接完整执行 Provider。
4. `RecoveryPolicy` 对 crash、timeout、provider offline、verification fail 给出 retry/replan/escalate/stop 决策。
5. `RequirementSupervisor` 是单写者；通过 lease、版本号和 reconciliation 管理 Worker、并发槽位、依赖与重启恢复。
6. Worker 只能产生 `candidate_complete`、`blocked` 或 `failed`，不能直接完成 Task/Requirement。
7. 默认策略为可单测的本地规则实现；以后可替换为插件或 Agent 策略，但策略输出必须结构化并持久化。
8. 冻结最小 `VerificationPlan`、`VerificationReceiptEnvelope` 与 `VerificationExecutorPort`，供 Phase 3 在没有完整 Provider 前生成结构化、新鲜的合并证据。
9. Supervisor 与 Worker 使用不同权限域；Worker 只能写自己的 Task worktree 与事件入口，不能直接写 canonical `.workspace`、Gate/Activation、控制面程序或 trust policy。

### 退出标准

- [ ] `P2-AC-01` direct 与 DAG 两条路径都有 E2E。
- [ ] `P2-AC-02` DAG 循环、未知依赖、重复 dispatch、并发超限和租约过期均被拒绝或收敛。
- [ ] `P2-AC-03` 模型路由只选择 Runtime 实际报告可用的模型和能力。
- [ ] `P2-AC-04` Worker crash/restart、retry budget、replan 和人工阻塞都有确定性测试。
- [ ] `P2-AC-05` Policy Provider 可替换，Core 测试使用至少两个 fake 实现。
- [ ] `P2-AC-06` Task Provider 离线时本地执行不丢失，恢复后投影幂等收敛。
- [ ] `P2-AC-07` 恶意/失控 Worker 无法直接写 Gate、Activation、canonical Workspace 或控制面策略；越权、控制面离线和重启恢复测试全部失败关闭。
- [ ] `P2-AC-08` CommonQualityGate 与独立 Phase 2 Review 全部 PASS。
