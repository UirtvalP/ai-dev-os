# 路线图

## 阶段 0 — 仓库基础

- [x] 明确 V1 架构与参考项目边界
- [x] 初始化 Python 包、测试、模板与 Skill 目录
- [x] 定义核心模型和 Provider 协议
- [x] 建立 README 与 Roadmap

## 阶段 1 — 工作区基础

- [x] 实现 `workspace new`
- [x] 实现 `workspace status`
- [x] 生成稳定 Requirement ID
- [x] 初始化人类可读的 Workspace 文件
- [x] 为创建与读取流程添加测试

验收：可以在 `.workspace/REQ-*` 中创建并读取一个完整 Requirement Workspace。

## 阶段 2 — 恢复、检查点与交接

- [x] 实现 `workspace resume`
- [x] 生成精简 Context Snapshot
- [x] 实现 `workspace checkpoint`
- [x] 实现 `workspace handoff`
- [x] 记录多个可替换 Session

验收：新 Codex Thread 不读取旧对话，也能从 Handoff 和 Workspace 恢复下一步。

## 阶段 3 — dashi-taskboard 与 Git

- [x] 实现 Local Git Adapter
- [x] 固化 dashi-taskboard / `taskctl` JSON 接口
- [x] 实现 Task 创建、状态同步与关联接口
- [x] 关联 Requirement、Task、Thread、branch 与 worktree
- [x] 支持无 dashi 时的本地降级运行

说明：已在开发机上使用 dashi-taskboard v1.1.9 完成真实 `taskctl` 验收，包括项目与
Issue 创建、查询、评论、乐观版本状态更新、Git 上下文、完整 Codex Thread Binding，
以及 Workspace Restore / Review 联合验证。

验收：Task 状态与 Git 上下文可恢复，并且外部系统暂时不可用时不会破坏 Workspace。

## 阶段 4 — 自适应工作流

- [x] 实现 tiny / normal / complex / research 路由
- [x] 记录工作流升级原因
- [x] 建立 verification 与 review gate
- [x] 禁止 Agent 自主将 Requirement 标为 done

验收：小修复不走重型流程，跨模块或高风险工作可基于证据升级。

## 阶段 5 — Codex Skill 演示

- [x] Skill 自动检测并恢复当前 Workspace
- [x] 跑通 Thread A handoff → Thread B resume 演示
- [x] 完成 V1 Acceptance Criteria
- [x] 发布首个可安装版本

验收：`workspace-orchestrator` 已通过统一 Skill 管理器链接到 Codex、Cursor 和 Claude；
`ai_dev_os-0.1.0-py3-none-any.whl` 已完成隔离导入和 CLI 冒烟测试。

## 阶段 6 — Hook-first 交付与可靠性加固

- [x] 提供 `in_review` 经用户明确确认进入 `done` 的确定性 CLI/API
- [x] 从 Git 关联 worktree 发现并共享主 `.workspace`
- [x] dashi/taskctl 离线时本地降级，并对 Session/Task 绑定失败持续重试
- [x] 为 JSON/Markdown 复合更新增加跨进程文件锁与原子替换
- [x] 已安装 wheel 的 `ai-dev-os init` 直接交付 Hook，不依赖目标源码或 `.venv`
- [x] finalize 返回具体 blockers，验证命令支持超时与配置校验

验收：review→用户确认→done、worktree 发现、Provider 离线恢复、多进程写入、wheel 安装后新项目
Hook、finalize blockers、验证超时与无效配置均有自动回归测试。

## 阶段 7 — 默认 dashi 与用户审查闭环

- [x] `ai-dev-os init` 初始化项目级 dashi 默认配置
- [x] 新 Requirement 默认继承 dashi，并允许用户显式关闭 Provider
- [x] finalize 创建具有稳定身份的专用 Requirement Review Task
- [x] dashi Review 卡 `done` 作为显式批准，普通开发 Task `done` 不推导批准
- [x] dashi Review 卡留言并退回 `in_progress`，或 `workspace request-changes`，恢复开发
- [x] Provider 离线时保留退回修改状态并在后续同步收敛
- [x] 明确并测试已绑定独立 worktree 的多 Requirement 并发边界

验收：用户可以只在 dashi 面板完成批准或退回；Core 仍只依赖 TaskProvider 契约，且没有
用户明确动作时 Requirement 与 Review Task 均不会自动进入 `done`。

## 阶段 8 — 确定性 Review Packet 与并发收敛

- [x] 从 Workspace、开发 Task 与 Git 事实生成结构化 Review Packet，不依赖 LLM 或聊天历史
- [x] 通过 TaskProvider 幂等更新 dashi Review 卡正文，不重复刷评论
- [x] 持久化并核验 packet revision/fingerprint，拒绝旧 revision 的 `done` 审批
- [x] Packet 缺字段或发布失败时阻断 review-ready，并保留可重试状态
- [x] 为 Requirement ID 创建、Review 同步和 SessionEnd 强杀恢复补齐并发测试

验收：任务面板 Review 卡可独立验收；证据变化会生成新 revision，重复发布不产生额外写入，
并发 Hook 的批准/退回只应用一次，SessionEnd 超时终止不会遗留阻塞后续本地开发的锁。

## 后续规划

- Multica、多 Agent 调度与自动并行 Agent（待 Task Graph 与完整 Worktree 隔离成熟）
- 跨项目知识库与 Obsidian / Markdown Knowledge Provider（待知识候选审核模型成熟）
- GitHub Issues / Linear Task Provider
- Remote Runtime、Web UI、自动 PR 与 CI feedback loop
