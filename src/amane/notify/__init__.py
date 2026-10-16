"""任务通知: 模板渲染 + 渠道发送 (内置功能, 非插件)."""

from .service import NotificationService
from .templates import json_escape, render

__all__ = ["NotificationService", "json_escape", "render"]
