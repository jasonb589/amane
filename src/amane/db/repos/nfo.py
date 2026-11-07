"""NFO 实际输出位置, 删除媒体索引时保留, 删除所属库或影片时移除."""

from pathlib import Path

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col, select

from ...utils.path import nfc_path, path_key
from ..models import NfoOutput
from ..repo_types import _utcnow
from .base import RepositoryMixinBase


class NfoRepoMixin(RepositoryMixinBase):
    async def get_nfo_output(self, nfo_path: Path) -> NfoOutput | None:
        async with self._session() as session:
            return (await session.exec(select(NfoOutput).where(NfoOutput.path_key == path_key(nfo_path)))).first()

    async def list_nfo_outputs(self, metadata_id: int) -> list[NfoOutput]:
        async with self._session() as session:
            return list((await session.exec(select(NfoOutput).where(NfoOutput.metadata_id == metadata_id))).all())

    async def remember_nfo_output(self, metadata_id: int, library_id: int, video_path: Path, nfo_path: Path) -> bool:
        """同一 NFO 只能属于一个影片; 重复整理更新同一影片的位置."""
        values = {
            "metadata_id": metadata_id,
            "library_id": library_id,
            "video_path": nfc_path(str(video_path)),
            "nfo_path": nfc_path(str(nfo_path)),
            "path_key": path_key(nfo_path),
            "updated_at": _utcnow(),
        }
        stmt = insert(NfoOutput).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["path_key"],
            set_={key: value for key, value in values.items() if key != "metadata_id"},
            where=col(NfoOutput.metadata_id) == metadata_id,
        )
        async with self._session() as session:
            await session.exec(stmt)
            await session.commit()
            output = (await session.exec(select(NfoOutput).where(NfoOutput.path_key == values["path_key"]))).one()
            return output.metadata_id == metadata_id
