# ADR-004：原生 Agent 生命周期与 AI Dev OS 完全隔离

## 状态

P3 已采用，作为后续版本长期回归门禁。

## 决策

- `ai-dev-os project add` 不创建或修改 Codex、Claude、Cursor 会自动消费的生命周期文件。
- AI Dev OS 的 `.ai-dev-os.json` 与 `.workspace/` 只由显式 Workbench/CLI 操作读取，原生 Agent 不会被动触发。
- `ai-dev-os project isolation-check` 对项目级 `AGENTS.md`、Codex hooks、Claude settings hooks 与 Cursor rules 执行 fail-closed 审计。
- 用户自己的原生 Agent 配置必须逐字节保留；非 AI Dev OS Hook/规则不构成违规。
- V1 legacy 项目在 P10 迁移前会明确报告 isolation failure，不能冒充已隔离。

## 保证边界

本门禁证明仓库级静态入口不会自动 bootstrap、建 Requirement/Task/Execution、绑定 Session、注入 Context 或触发 finalize，也不会通过 AI Dev OS 改写 model/reasoning/approval/sandbox。用户级第三方配置与 Agent 产品自身行为不属于项目注册器可控制范围。
