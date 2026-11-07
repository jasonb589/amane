from pydantic import BaseModel


class NotifyTestResponse(BaseModel):
    ok: bool
    message: str
