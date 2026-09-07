## 9. Phase 3 — Git Integration / Merge Gate（AID-172）

### 实现范围

1. 新增 `GitWorkspaceProvider`，幂等创建/恢复/释放每个 Task 的 branch、worktree 与 lease。
2. 新增 `IntegrationProvider` 与 Requirement integration branch；Worker 不直接写 `main`。
3. 建立 merge queue：检查候选 commit、基线、工作树清洁度、冲突、Task Gate、Requirement Gate 和证据新鲜度。
4. 合并前运行 integration verification；合并后记录 `MergeReceipt` 并运行 post-merge verification。
5. 失败时保留可诊断 worktree/branch，不自动删除用户改动；恢复操作幂等。
6. `main` 存在未提交修改、HEAD 漂移或远端落后时拒绝集成。
7. 使用 `LegacyVerificationAdapter` 包装现有执行器，生成绑定 candidate SHA/tree、环境、命令列表哈希与结果的最小 Receipt；不得从 Markdown 展示文本反推事实。Phase 4 在不改变 Merge Gate 接口的前提下替换完整 Provider并重跑本阶段测试。

### 退出标准

- [ ] `P3-AC-01` 并行 Task 永不共享 branch/worktree；重启后租约可恢复。
- [ ] `P3-AC-02` 未验证、陈旧证据、冲突、脏 main、非预期 base 和重复 merge 都有负向测试。
- [ ] `P3-AC-03` 只有全部 Task accepted 且 Requirement Review Gate 通过后才签发 `IntegrationAuthorization`。
- [ ] `P3-AC-04` 同一 merge request 重试只产生一个可识别结果；ref update 前后崩溃均可 reconcile。
- [ ] `P3-AC-05` merge 副作用前重新解析 expected main SHA，授权后的 ref 漂移会失败关闭。
- [ ] `P3-AC-06` post-merge 失败不会生成 CompletionToken 或触发部署。
- [ ] `P3-AC-07` CommonQualityGate 与独立 Phase 3 Review 全部 PASS。
