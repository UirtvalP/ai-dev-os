## 12. Phase 6 — main-only Deployment Gate（AID-174）

### 实现范围

1. 新增 `DeploymentProvider`、`DeploymentPolicy`、`DeploymentReceipt` 与环境注册表。
2. 默认硬门禁要求：目标 commit 等于本地受保护 `refs/heads/main` 的解析 HEAD，并按项目策略确认远端 main 一致。
3. 部署前要求当前 commit 的 `MergeReceipt`、post-merge `VerificationReceipt` 与 `DeploymentAuthorization` 均有效；此时不得要求尚未签发的 `RequirementCompletionToken`。
4. 部署命令由 Provider 执行；Core 不包含平台脚本、密钥或环境特定逻辑。
5. 幂等键为 project/environment/commit/provider version；重复请求返回同一收据或明确状态。
6. 保存发起者、目标环境、main 证明、验证证据、开始/结束时间、结果和回滚指引。

### 退出标准

- [ ] `P6-AC-01` feature、integration、detached HEAD、旧 main commit、脏/漂移 main 均被拒绝。
- [ ] `P6-AC-02` 缺失或陈旧 Merge/post-merge Verification/Deployment Authorization 时拒绝部署。
- [ ] `P6-AC-03` 成功、失败、超时、重复请求和 Provider 离线均有测试和可恢复状态。
- [ ] `P6-AC-04` 只有 main-only Gate 能调用实际 Deployment Provider；副作用前再次解析 main，TOCTOU 漂移时拒绝。
- [ ] `P6-AC-05` 无需部署与 deployment-required 两条完整 E2E 均正确签发 CompletionToken。
- [ ] `P6-AC-06` CommonQualityGate、真实本地 dry-run E2E 与独立安全 Review 全部 PASS。
