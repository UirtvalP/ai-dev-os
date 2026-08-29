"""源码仓库兼容入口；正式 Hook 实现在可安装模块中。"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src"))

main = import_module("workspace_orchestrator.codex_hook").main


if __name__ == "__main__":
    raise SystemExit(main())
