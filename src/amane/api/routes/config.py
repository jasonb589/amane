from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ...config import HotSettings
from ...config.manager import NotifyChannelConfig
from ...events import Event, EventType
from ..deps import ConfigDep, PluginManagerDep, RuntimeDep

logger = structlog.get_logger()

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_config(config: ConfigDep) -> HotSettings:
    return config.hot


@router.patch("")
async def update_config(req: dict[str, Any], config: ConfigDep, runtime: RuntimeDep) -> HotSettings:
    """先用当前会话的插件目录校验新路由和插件配置, 再持久化."""
    changed_sections = list(req.keys())
    logger.info("config update requested", sections=changed_sections)

    try:
        preview = config.preview(req)
        if runtime.plugin_manager is not None:
            runtime.plugin_manager.validate_hot_settings(preview, require_available=False)
        config.update(req)
    except (KeyError, ValidationError, ValueError) as e:
        logger.warning("config update rejected", error=str(e), sections=changed_sections)
        raise HTTPException(status_code=422, detail=str(e)) from e

    await runtime.apply_rebuild()
    logger.info("runtime rebuilt after config update", sections=changed_sections)

    await runtime.event_bus.broadcast(Event(type=EventType.LOG, data={"type": "config.updated"}))

    return config.hot


@router.get("/schema")
async def get_config_schema(plugin_manager: PluginManagerDep) -> dict:
    return plugin_manager.augment_config_schema(HotSettings.model_json_schema())


@router.post("/notify/test")
async def test_notify(req: dict[str, Any], runtime: RuntimeDep) -> dict[str, Any]:
    """用界面里正在编辑的渠道配置发一条测试通知 (不必先保存)."""
    try:
        channel = NotifyChannelConfig.model_validate(req)
    except (KeyError, ValidationError, ValueError) as e:
        logger.warning("notify test rejected", error=str(e))
        raise HTTPException(status_code=422, detail=str(e)) from e
    ok, message = await runtime.notifier.send_test(channel)
    logger.info("notify test sent", ok=ok, message=message)
    return {"ok": ok, "message": message}
