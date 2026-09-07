# P8 Supervisor Watchdog 迁移说明

## 新入口

```powershell
ai-dev-os workbench supervise REQ-021 --root <workspace>
```

命令执行一次有限、确定性的扫描，写入 `.workspace/REQ-ID/supervisor-watchdog.json`。重复运行只更新观察 revision 和当前信号，不执行 Agent 副作用。

## 兼容性

- 既有 `orchestration/supervisor` 继续负责 Task 执行权威，不迁移或重写其状态。
- Main Agent source fingerprint 同时包含既有 Supervisor 和 Watchdog 状态。
- 没有 Watchdog 文件的旧 Requirement 按“无信号”读取。

## 回滚

回滚 P8 提交后可保留或删除 `supervisor-watchdog.json`；其不参与旧调度、验证、Gate 或 Git 权威。

