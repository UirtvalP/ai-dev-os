# ADR-011：默认退出原生 Agent 生命周期

## 决策

`ai-dev-os init` 只注册 Workbench 项目、初始化本地 Requirement 存储和明确需要进入 Git 的 Intent/ignore 配置。它不写 `AGENTS.md`、不创建 `.codex/hooks.json`、不自动启动 Dispatcher。

AI Dev OS 启动 Agent 时，由 ExecutionSpec Prompt 注入 Requirement、Task、Goal、Intent、Acceptance 与结果要求；这些规则只约束对应 Execution，不再通过项目级 AGENTS 影响原生 Codex、Claude Code 或 Cursor。

旧 lifecycle Hook 代码暂留为迁移兼容，但不再是主架构入口。唯一新装入口是操作者显式执行 `ai-dev-os integration enable codex-hooks`；该 Hook 只把 prompt 中明确指定 `REQ-ID` 的原生 Codex Thread 映射为一等 Execution，不执行 bootstrap、finalize、Stop 收尾或 SessionEnd detach。

## 数据与回滚

`ai-dev-os migrate` 只删除 AI Dev OS 自己管理的 AGENTS 区块和 Hook 命令，保留其他内容；旧 Session 幂等映射为 `source=legacy-thread-binding` 的 Execution，原 sessions.json 与全部 Requirement/Git 事实不修改。

如需退出可选 Codex 集成，执行 `ai-dev-os integration disable codex-hooks`。该命令保留用户 Hook 与已导入 Execution 历史。
