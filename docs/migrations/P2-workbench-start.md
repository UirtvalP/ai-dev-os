# P2 Workbench 主动启动迁移说明

## 新项目注册

```powershell
ai-dev-os project add .
```

该命令不会创建 `.codex/hooks.json`、不会写 `AGENTS.md`、不会启动 Dispatcher。V1 的 `ai-dev-os init` 在 P10 前继续兼容旧项目。

## 可见 Demo

```powershell
ai-dev-os workbench demo REQ-021 --task TASK-P2-DEMO
```

可用 `workspace execution show EXE-xxxxxx` 与 `workspace execution events EXE-xxxxxx` 核对 Requirement、Task、Execution、Runtime、Session、Turn 与完成事件。

## 当前边界

- P2 建立主动启动所有权与确定性失败记录，不实现 Main Agent、路由策略、Console 或 Supervisor。
- Demo Runtime 是非权威替身；真实 Runtime 通过同一 `StandardAgentRuntimePort` 注入，并在后续 Console 常驻进程中持有生命周期。
