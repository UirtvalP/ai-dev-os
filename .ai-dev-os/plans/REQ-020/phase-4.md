## 10. Phase 4 — Verification Provider（AID-173）

### 实现范围

1. 把固定命令执行拆成 `VerificationPlannerProvider` 与 `VerificationExecutorProvider`。
2. 默认本地 Provider 兼容当前 `pyproject.toml` 命令，并增加 suite、timeout、working directory、环境白名单和 artifact 约束。
3. `VerificationReceipt` 绑定 plan fingerprint、commit SHA、环境摘要、耗时、退出码、结果摘要和 artifact digest。
4. 支持 unit/type/lint/integration/e2e/security/custom suite；名称与实际工具一致，不再把 lint 冒充 type check。
5. 支持 fail-fast 与 collect-all 策略，但 Gate 明确要求的 suite 缺失时必须失败。
6. 证据变化、commit 变化或环境不匹配时自动判定旧 Receipt 失效。
7. 权威 Receipt 使用签名 envelope：不同 OS principal、远端 CI 或等价受保护 attestor 自行读取受保护 policy 和 exact-SHA Suite、执行/查询事实并签发；Worker 不能提交 argv/result、读取签名密钥、替换 trust root 或用仓库自带公钥降级信任。

### 退出标准

- [ ] `P4-AC-01` Provider 替换不修改 Core；fake/local 两类实现通过同一 contract suite。
- [ ] `P4-AC-02` timeout、进程不可用、输出限长、artifact 缺失、陈旧 commit 与部分通过均有测试。
- [ ] `P4-AC-03` Phase Gate 能声明必需 suites，未执行或失败时不得推进。
- [ ] `P4-AC-04` Review Packet 使用结构化 Receipt，不再从展示文本反推验证事实。
- [ ] `P4-AC-05` Phase 3 的 Legacy Receipt 兼容迁移及全部 Merge Gate 回归通过。
- [ ] `P4-AC-06` unsigned、自签、上下文重放、policy 降级、PATH/PYTHONPATH 注入、假 GitHub reader、密钥/网络越权与 attestor 离线全部被拒绝；签名绑定 project/Requirement/Phase/SHA/tree/suite/policy/run/attempt/result/artifact。
- [ ] `P4-AC-07` CommonQualityGate 与独立 Phase 4 Review 全部 PASS。
