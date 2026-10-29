"""把 r18 PostgreSQL dump 导入用户自备的 PG 实例.

全程在临时库进行; schema 校验通过后才原子换名为正式库.
导入失败不允许污染线上镜像; 坏 dump 最多导致一次被跳过的导入.
依赖外部 ``psql`` (容器需 postgresql-client).
"""

import asyncio
import gzip
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import quote
from xml.etree.ElementTree import ElementTree, ParseError

import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from ...net.errors import RequestError
from .repository import R18Repository

if TYPE_CHECKING:
    from ...config import R18Config
    from ...net.http import WebClient

logger = structlog.get_logger()

_LATEST_TOKEN = "{latest}"
"""下载地址里的占位符: 运行时换成桶内最新的 ``.sql.gz``, 免去每周手改日期."""

_MAX_LIST_PAGES = 10
"""列举分页上限 (S3 单页最多 1000 个 key, 十页足够覆盖一年周更)."""


def _split_latest(url: str) -> tuple[str, str] | None:
    """``https://host/dumps/{latest}`` → ``("https://host", "dumps/")``; 无占位符返回 None."""
    index = url.find(_LATEST_TOKEN)
    if index < 0:
        return None
    head = url[:index]
    slash = head.rfind("/")
    if slash < 0:
        return None
    return head[:slash], head[slash + 1 :]


def parse_s3_listing(xml_text: str) -> tuple[list[tuple[str, str]], str | None]:
    """S3 列举响应 → ([(key, last_modified)], 下一页 token). 命名空间前缀一律削掉."""
    root = ElementTree.fromstring(xml_text)
    entries: list[tuple[str, str]] = []
    token: str | None = None
    truncated = False
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "Contents":
            key = modified = ""
            for child in node:
                name = child.tag.rsplit("}", 1)[-1]
                if name == "Key":
                    key = (child.text or "").strip()
                elif name == "LastModified":
                    modified = (child.text or "").strip()
            if key:
                entries.append((key, modified))
        elif tag == "IsTruncated":
            truncated = (node.text or "").strip().lower() == "true"
        elif tag == "NextContinuationToken":
            token = (node.text or "").strip() or None
    return entries, token if truncated else None


class RemoteMeta(NamedTuple):
    filename: str
    size: int
    etag: str | None
    file_url: str

    def same_as(self, other: RemoteMeta | None) -> bool:
        if other is None:
            return False
        if self.etag and other.etag:
            return self.etag == other.etag
        return self.filename == other.filename and self.size == other.size


class R18Importer:
    def __init__(self, config: R18Config, web_client: WebClient):
        self.config = config
        self.client = web_client

    async def _resolve_download_url(self) -> str | None:
        """把含 ``{latest}`` 的地址换成桶内最新的一份 dump; 普通地址原样返回."""
        url = self.config.download_url
        if not url:
            return None
        parts = _split_latest(url)
        if parts is None:
            return url

        root, prefix = parts
        newest: tuple[str, str] | None = None
        token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            listing = f"{root}/?list-type=2&max-keys=1000&prefix={quote(prefix)}"
            if token:
                listing += f"&continuation-token={quote(token)}"
            try:
                resp = await self.client.request("GET", listing)
            except RequestError as exc:
                logger.warning("r18 dump listing failed", error=exc.message)
                return None
            try:
                entries, token = parse_s3_listing(resp.text)
            except ParseError as exc:
                logger.warning("r18 dump listing unparsable", error=str(exc))
                return None
            for key, modified in entries:
                if not key.endswith(".sql.gz"):
                    continue
                if newest is None or modified > newest[1]:
                    newest = (key, modified)
            if token is None:
                break

        if newest is None:
            logger.warning("r18 dump listing empty", prefix=prefix)
            return None
        logger.info("r18 dump resolved", key=newest[0], modified=newest[1])
        return f"{root}/{newest[0]}"

    async def fetch_remote_meta(self) -> RemoteMeta | None:
        url = await self._resolve_download_url()
        if not url:
            return None

        try:
            resp = await self.client.request("HEAD", url, allow_redirects=False)
        except RequestError as exc:
            logger.warning("r18 download HEAD failed", error=exc.message)
            return None

        actual_url = resp.headers.get("Location") or url
        try:
            s3_resp = await self.client.request("HEAD", actual_url)
        except RequestError as exc:
            logger.warning("r18 dump HEAD failed", url=actual_url, error=exc.message)
            return None

        headers = s3_resp.headers
        return RemoteMeta(
            filename=actual_url.split("/")[-1] or "r18.sql.gz",
            size=int(headers.get("content-length", 0) or 0),
            etag=(headers.get("etag", "") or "").strip('"') or None,
            file_url=actual_url,
        )

    async def run(self, current_meta: RemoteMeta | None = None) -> tuple[bool, str, RemoteMeta | None]:
        # 与远程一致时跳过, 返回 success=True.
        if not self.config.dsn:
            return False, "r18.dsn 未配置", None

        admin_template = create_async_engine(self.config.admin_url("template1"), isolation_level="AUTOCOMMIT")
        try:
            # 探测远程 dump, 与已导入版本一致则跳过.
            meta = await self.fetch_remote_meta()
            if meta and meta.same_as(current_meta):
                logger.info("r18 dump unchanged, skip import", etag=meta.etag)
                return True, "", meta

            file_url = meta.file_url if meta else self.config.download_url
            filename = meta.filename if meta else "r18.sql.gz"
            if not file_url:
                return False, "无下载地址且未配置 download_url", None

            # 下载并解压.
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                archive = tmp_path / filename
                logger.debug("r18 import downloading", url=file_url, target=str(archive))
                if not await self.client.download(file_url, archive):
                    return False, "下载 dump 失败", None
                logger.debug("r18 dump downloaded", path=str(archive), size=archive.stat().st_size)

                sql_file = tmp_path / (archive.stem or "r18.sql")
                logger.debug("r18 import decompressing", src=str(archive), dst=str(sql_file))
                await asyncio.to_thread(_gunzip, archive, sql_file)
                logger.debug("r18 dump decompressed", path=str(sql_file))

                temp_db = await self._temp_db_name(admin_template)
                logger.debug("r18 import creating temp db", db=temp_db)
                async with admin_template.connect() as conn:
                    await _create_db(conn, temp_db)
                logger.debug("r18 import temp db created", db=temp_db)

                # 导入临时库并校验 schema.
                await self._psql_import(sql_file, temp_db)

                ok, verr = await self._validate(temp_db)
                if not ok:
                    logger.warning("r18 import schema validation failed", db=temp_db, error=verr)
                    async with admin_template.connect() as conn:
                        await _drop_db(conn, temp_db)
                    return False, f"schema 校验失败, 已回滚: {verr}", None
                logger.debug("r18 import schema validated", db=temp_db)

                # 校验通过后原子换名.
                logger.debug("r18 import swapping db", old=temp_db, new=self.config.db_name)
                async with admin_template.connect() as conn:
                    if await _db_exists(conn, self.config.db_name):
                        await _drop_db(conn, self.config.db_name)
                    await _rename_db(conn, temp_db, self.config.db_name)
                logger.debug("r18 import db swapped", old=temp_db, new=self.config.db_name)

                logger.debug("r18 import ensuring readonly role")
                await self._ensure_readonly_role(admin_template)

            logger.info("r18 import complete", db=self.config.db_name)
            return True, "", meta
        except Exception as e:
            logger.exception("r18 import failed", db=self.config.db_name, meta=meta)
            return False, f"导入异常: {e}", None
        finally:
            await admin_template.dispose()

    async def _temp_db_name(self, engine) -> str:
        # 临时库名用 PG clock, 避免客户端时钟不确定.
        async with engine.connect() as conn:
            epoch = (await conn.execute(text("SELECT (extract(epoch from clock_timestamp()) * 1000)::bigint"))).scalar()
        return f"{self.config.db_name}_import_{epoch}"

    async def _psql_import(self, sql_file: Path, db_name: str) -> None:
        # dump 含 COPY / 函数等, 必须用原生 psql -f.
        if not self.config.dsn:
            raise RuntimeError("r18.dsn 未配置")
        logger.info("r18 import psql starting", db=db_name, file=str(sql_file), size=sql_file.stat().st_size)
        url = make_url(self.config.dsn)
        cmd = [
            self.config.psql_path,
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            url.host or "localhost",
            "-p",
            str(url.port or 5432),
            "-U",
            url.username or "postgres",
            "-d",
            db_name,
            "-f",
            str(sql_file),
        ]
        env = os.environ.copy()
        if url.password:
            env["PGPASSWORD"] = url.password

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"psql 导入失败 (退出码 {proc.returncode}): {stderr.decode(errors='replace')[:2000]}")
        logger.info("r18 import psql done", db=db_name)

    async def _validate(self, db_name: str) -> tuple[bool, str]:
        # 任一探针失败即不兼容, 拒绝换名.
        logger.debug("r18 import validating schema", db=db_name, probes=len(R18Repository.schema_probes()))
        engine = create_async_engine(self.config.admin_url(db_name))
        try:
            async with engine.connect() as conn:
                for probe in R18Repository.schema_probes():
                    try:
                        await conn.execute(text(probe))
                    except Exception as e:
                        return False, f"探针失败 [{probe[:60]}...]: {e}"
            return True, ""
        finally:
            await engine.dispose()

    async def _ensure_readonly_role(self, admin_template) -> None:
        async with admin_template.connect() as conn:
            role = await _quote_ident(conn, self.config.read_user)
            db = await _quote_ident(conn, self.config.db_name)
            pw = await _quote_literal(conn, self.config.read_password)

            exists = (
                await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": self.config.read_user})
            ).scalar()
            if not exists:
                await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD {pw}"))
                logger.debug("r18 import readonly role created", role=self.config.read_user)
            await conn.execute(text(f"ALTER ROLE {role} SET statement_timeout = {self.config.read_timeout * 1000}"))

        db_engine = create_async_engine(self.config.admin_url(self.config.db_name), isolation_level="AUTOCOMMIT")
        try:
            async with db_engine.connect() as conn:
                await conn.execute(text(f"GRANT CONNECT ON DATABASE {db} TO {role}"))
                await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
                await conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}"))
                await conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}"))
        finally:
            await db_engine.dispose()


def _gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as f_in, open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


async def _quote_ident(conn: AsyncConnection, identifier: str) -> str:
    return (await conn.execute(text("SELECT quote_ident(:i)"), {"i": identifier})).scalar() or ""


async def _quote_literal(conn: AsyncConnection, literal: str) -> str:
    return (await conn.execute(text("SELECT quote_literal(:l)"), {"l": literal})).scalar() or ""


async def _db_exists(conn: AsyncConnection, db_name: str) -> bool:
    return (
        await conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db_name})
    ).scalar() is not None


async def _create_db(conn: AsyncConnection, db_name: str) -> None:
    ident = await _quote_ident(conn, db_name)
    await conn.execute(text(f"CREATE DATABASE {ident}"))


async def _drop_db(conn: AsyncConnection, db_name: str) -> None:
    await conn.execute(
        text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()"),
        {"d": db_name},
    )
    ident = await _quote_ident(conn, db_name)
    await conn.execute(text(f"DROP DATABASE IF EXISTS {ident}"))


async def _rename_db(conn: AsyncConnection, old: str, new: str) -> None:
    await conn.execute(
        text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()"),
        {"d": old},
    )
    old_i = await _quote_ident(conn, old)
    new_i = await _quote_ident(conn, new)
    await conn.execute(text(f"ALTER DATABASE {old_i} RENAME TO {new_i}"))
