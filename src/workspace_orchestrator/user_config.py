"""AI Dev OS 用户级配置路径。"""

from __future__ import annotations

from pathlib import Path

USER_CONFIG_DIR_NAME = ".ai-dev-os"
USER_PRINCIPLES_NAME = "USER_PRINCIPLES.md"
USER_PRINCIPLES_DISPLAY_PATH = f"~/{USER_CONFIG_DIR_NAME}/{USER_PRINCIPLES_NAME}"

DEFAULT_USER_PRINCIPLES = """# 用户原则

记录跨项目、长期有效的工作偏好。除非当前需求明确记录合理例外，否则这些原则均为强制约束。

## 默认原则

- 优先采用能够安全完成任务的最轻工作流。
- 保留现有用户文件和人类可读的本地状态。
- 避免未经需求证实的抽象、集成与自动化。
- 技术正确但违反已记录意图的修改不算完成。
"""


def user_config_root() -> Path:
    """返回当前操作系统用户的 AI Dev OS 配置目录。"""

    return Path.home() / USER_CONFIG_DIR_NAME


def user_principles_path() -> Path:
    """返回唯一的用户级原则文件路径。"""

    return user_config_root() / USER_PRINCIPLES_NAME
