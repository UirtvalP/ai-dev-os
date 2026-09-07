# P10 Legacy Hook 降级/移除

## 新项目

```powershell
ai-dev-os init D:\path\to\project
ai-dev-os project isolation-check D:\path\to\project
```

默认结果必须没有 AI Dev OS 管理的 `AGENTS.md` 区块和 `.codex/hooks.json`，原生 Agent 使用不受影响。

## 旧项目

先备份或提交项目文件，再执行：

```powershell
ai-dev-os migrate D:\path\to\project
```

迁移会预检后移除旧 lifecycle 接入面，并映射 legacy Execution；不会删除或改写 Requirement、Intent、Task、Checkpoint、Handoff、Verification、Gate、Decision、Git state 与原 Session 数据。命令可幂等重跑。

## 可选 Codex 导入

```powershell
ai-dev-os integration enable codex-hooks --root D:\path\to\project
ai-dev-os integration disable codex-hooks --root D:\path\to\project
```

这是显式兼容能力，不是 AI Dev OS 核心 Runtime。
