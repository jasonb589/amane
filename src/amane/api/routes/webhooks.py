import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from ..deps import RuntimeDep
from ..models.webhooks import CloudDriveNotifyRequest

logger = structlog.get_logger()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_EMPTY_BODY = b"{}"


@router.post("/clouddrive", status_code=204)
async def clouddrive_notify(request: Request, runtime: RuntimeDep) -> Response:
    """CloudDrive file_system_watcher 回调. 鉴权与其它 /api 相同 (Bearer).

    CloudDrive2 不保证带 ``Content-Type: application/json``: 缺这个头时 FastAPI 不会把请求体
    交给模型, 整条通道直接 422 (上游 issue #384). 因此这里不看声明类型, 直接把原始 body 当
    JSON 解析, 只有真的解析不出来才回 422.
    """

    raw = (await request.body()).strip() or _EMPTY_BODY
    try:
        req = CloudDriveNotifyRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    if runtime.watcher_service is None:
        logger.warning("clouddrive webhook ignored: watcher service unavailable")
        return Response(status_code=204)
    if not req.data:
        return Response(status_code=204)
    runtime.watcher_service.submit_clouddrive([item.to_change() for item in req.data])
    return Response(status_code=204)
