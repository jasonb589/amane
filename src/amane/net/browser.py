"""浏览器后端: 协议、按来源会话复用与失败映射.

``HttpClient`` 的渲染路径经 ``BrowserClient`` 协议调用; 本地引擎与外部 solver 服务可互换.
后端不感知爬虫层: 结果以 ``BrowserPageResult`` 返回, 失败语义由 ``RequestFailure`` 表达, 由
``HttpClient`` 统一分类. 协议中的 ``timeout`` 与 ``BrowserPool`` 的超时参数均为毫秒 (与 Playwright 一致).
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, AsyncExitStack, ExitStack
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

import structlog

from ..enums import BrowserBackendName
from .bsk import BskBackend
from .errors import FailureKind, FailureReason, RequestError, RequestFailure, classify_block
from .lifetime import AsyncLifetime
from .recording import skip_body_recording

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from .http import WebClient

logger = structlog.get_logger()

BrowserPageResult = tuple[str | None, RequestFailure | None]
"""(html, failure): 成功时 failure 为 None, 失败时 html 为 None."""

# 挑战页轮询间隔与浏览器空闲关闭时限 (秒).
_CHALLENGE_POLL_S = 1.0
_IDLE_TIMEOUT_S = 600.0
# 同一后端的并发页数上限. 限速由 RateLimiters 承担, 这里只约束浏览器资源.
_MAX_CONCURRENCY = 2
# solver 的 HTTP 调用在服务端等待挑战之外留出的余量 (秒), 以及会话建立 / 销毁的调用时限.
_SOLVER_HTTP_SLACK_S = 10.0
_SOLVER_SESSION_TIMEOUT_S = 15.0


class BrowserBackend(Protocol):
    """单个浏览器引擎."""

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
        network_idle_timeout: float | None = None,
    ) -> BrowserPageResult: ...

    async def close(self) -> None: ...


class BrowserClient(Protocol):
    """``HttpClient`` 依赖的浏览器通道: 按后端名解析实际引擎, 调用方不感知实现."""

    def resolve(self, override: BrowserBackendName | None) -> BrowserBackendName | None: ...

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        backend: BrowserBackendName | None = None,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
        network_idle_timeout: float | None = None,
    ) -> BrowserPageResult: ...


class BrowserPool:
    """按配置解析并缓存后端实例; 引擎惰性启动, ``off`` 不产生任何实例.

    来源覆盖优先于全局: ``SiteConfig.browser_backend`` 为 ``off`` 表示该来源显式禁用浏览器.
    solver 经注入的 HTTP 通道出站, 因此复用本实例的调用方在热重建后须调用 ``rebind_web_client``.
    """

    def __init__(
        self,
        *,
        default_backend: BrowserBackendName,
        solver_url: str,
        proxy: str | None,
        web_client: WebClient,
        timeout_ms: float,
        bsk_path: str | None = None,
    ) -> None:
        self._default = default_backend
        self._solver_url = solver_url
        self._proxy = proxy
        self._web = web_client
        self._timeout_ms = timeout_ms
        self._bsk_path = bsk_path
        self._instances: dict[BrowserBackendName, BrowserBackend] = {}
        self._closed = False
        self._lifetime = AsyncLifetime(self._close)
        self._web_lease = web_client.lease()
        self._web_lease.__enter__()

    def resolve(self, override: BrowserBackendName | None) -> BrowserBackendName | None:
        """来源覆盖优先; 覆盖为 ``off`` 表示显式禁用, 结果为 None."""
        if override is not None:
            return None if override is BrowserBackendName.OFF else override
        return None if self._default is BrowserBackendName.OFF else self._default

    def rebind_web_client(self, web_client: WebClient) -> None:
        """先持有新通道再释放旧通道; 进行中的 solver 操作另持有自己的通道租约."""
        with self.lease():
            replacement = web_client.lease()
            replacement.__enter__()
            previous, self._web_lease = self._web_lease, replacement
            self._web = web_client
            previous.__exit__(None, None, None)

    def lease(self) -> AbstractContextManager[None]:
        return self._lifetime.lease()

    def retire(self) -> None:
        self._lifetime.retire()

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        backend: BrowserBackendName | None = None,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
        network_idle_timeout: float | None = None,
    ) -> BrowserPageResult:
        name = self.resolve(backend)
        if name is None:
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="browser backend disabled")
        with ExitStack() as operation:
            try:
                operation.enter_context(self.lease())
            except RuntimeError:
                return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="browser pool closed")
            operation.enter_context(self._web.lease())
            instance = self._instances.get(name)
            if instance is None:
                instance = self._create(name)
                self._instances[name] = instance
            return await instance.get_page(
                url,
                scope=scope,
                cookies=cookies,
                headers=headers,
                wait_for=wait_for,
                timeout=timeout if timeout is not None else self._timeout_ms,
                network_idle_timeout=network_idle_timeout,
            )

    def _create(self, name: BrowserBackendName) -> BrowserBackend:
        match name:
            case BrowserBackendName.PATCHRIGHT:
                return PatchrightBackend(proxy=self._proxy, default_timeout=self._timeout_ms)
            case BrowserBackendName.CAMOUFOX:
                return CamoufoxBackend(proxy=self._proxy, default_timeout=self._timeout_ms)
            case BrowserBackendName.BSK:
                if self._proxy:
                    logger.warning("bsk backend ignores global proxy", proxy=self._proxy)
                return BskBackend(bsk_path=self._bsk_path, default_timeout=self._timeout_ms)
            case BrowserBackendName.SOLVER:
                return SolverBackend(
                    web_provider=lambda: self._web,
                    url=self._solver_url,
                    default_timeout=self._timeout_ms,
                    proxy=self._proxy,
                )
            case _:
                raise ValueError(f"unsupported browser backend: {name}")

    async def close(self) -> None:
        await self._lifetime.aclose()

    async def _close(self) -> None:
        self._closed = True
        instances = list(self._instances.values())
        self._instances.clear()
        try:
            results = await asyncio.gather(*(instance.close() for instance in instances), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            # solver 销毁远端会话仍需 HTTP 通道, 必须最后释放池对该通道的持有.
            self._web_lease.__exit__(None, None, None)


class _ChallengeUnresolved(Exception):
    """导航结束仍在挑战页: 由 ``_LocalBackend`` 转成带原因的失败."""


class _BackendClosed(Exception):
    """后端已关闭: 拒绝重新启动引擎."""


class _PageUnreadable(Exception):
    """等待期内未读到任何正文: 页面崩溃或已关闭, 与挑战未解决区分."""

    def __init__(self, error: BaseException | None) -> None:
        super().__init__(str(error) if error is not None else "page content unreadable")
        self.error = error


_WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class _Cookie(TypedDict, total=False):
    """playwright 系 ``SetCookieParam`` 的结构镜像.

    引擎是可选依赖, 协议不能直接引用其类型; 字段须与引擎定义保持一致才能结构匹配.
    """

    name: str
    value: str
    url: str | None
    domain: str | None
    path: str | None
    expires: float | None
    httpOnly: bool | None
    secure: bool | None
    sameSite: Literal["Lax", "None", "Strict"] | None
    partitionKey: str | None


class _PageLike(Protocol):
    """``_LocalBackend`` 使用的页面能力: 导航, 读取正文与等待选择器."""

    async def goto(self, url: str, *, timeout: float, wait_until: _WaitUntil) -> object: ...

    async def content(self) -> str: ...

    async def wait_for_load_state(
        self, state: Literal["load", "domcontentloaded", "networkidle"], *, timeout: float
    ) -> object: ...

    async def wait_for_selector(self, selector: str, *, timeout: float) -> object: ...

    async def close(self) -> None: ...


class _ContextLike(Protocol):
    """``_LocalBackend`` 使用的 context 能力: 注入 cookie / 请求头, 创建页面."""

    async def add_cookies(self, cookies: Sequence[_Cookie]) -> None: ...

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None: ...

    async def new_page(self) -> _PageLike: ...


class _BrowserLike(Protocol):
    """``_LocalBackend`` 使用的浏览器能力: 按来源创建独立 context."""

    async def new_context(self) -> _ContextLike: ...


class _LocalBackend(ABC):
    """playwright 系本地引擎共用: 会话复用, 挑战等待, 并发与空闲释放.

    每个来源一个 context: 同一来源的连续请求复用 clearance; 不同来源互不污染 cookie.
    返回给调用方的正文仍由 ``HttpClient`` 二次分类, 这里只等待挑战消解.
    """

    def __init__(self, *, proxy: str | None, default_timeout: float, idle_timeout: float = _IDLE_TIMEOUT_S) -> None:
        self._proxy = proxy
        self._default_timeout = default_timeout
        self._idle_timeout = idle_timeout
        self._stack: AsyncExitStack | None = None
        self._browser: _BrowserLike | None = None
        self._contexts: dict[str, _ContextLike] = {}
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._launch_lock = asyncio.Lock()
        self._active = 0
        self._idle_gen = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._closed = False

    @abstractmethod
    async def _launch(self, stack: AsyncExitStack) -> _BrowserLike:
        """启动浏览器, 并把退出清理压入 ``stack``."""

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
        network_idle_timeout: float | None = None,
    ) -> BrowserPageResult:
        effective = timeout if timeout is not None else self._default_timeout
        self._active += 1
        self._schedule_idle()
        try:
            async with self._semaphore:
                return await self._fetch(
                    url,
                    scope=scope,
                    cookies=cookies,
                    headers=headers,
                    wait_for=wait_for,
                    timeout=effective,
                    network_idle_timeout=network_idle_timeout,
                )
        finally:
            self._active -= 1

    async def _fetch(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        wait_for: str | None,
        timeout: float,
        network_idle_timeout: float | None,
    ) -> BrowserPageResult:
        page: _PageLike | None = None
        try:
            context = await self._context(scope)
            # 引擎启动 (含 camoufox 首次下载) 不计入单次渲染时限.
            deadline = time.monotonic() + timeout / 1000
            if cookies:
                context_cookies: list[_Cookie] = [
                    {"name": name, "value": value, "url": url} for name, value in cookies.items()
                ]
                await context.add_cookies(context_cookies)
            if headers:
                await context.set_extra_http_headers(dict(headers))
            page = await context.new_page()
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            content = await self._settle_challenge(page, deadline)
            if network_idle_timeout is not None:
                content = await self._idle_content(page, content, network_idle_timeout, deadline)
            if wait_for is not None:
                remaining = max(1.0, (deadline - time.monotonic()) * 1000)
                await page.wait_for_selector(wait_for, timeout=remaining)
                content = await page.content()
            return content, None
        except _BackendClosed:
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="browser backend closed")
        except _PageUnreadable as exc:
            if exc.error is not None and _is_timeout(exc.error):
                return None, RequestFailure(kind=FailureKind.TIMEOUT, message=f"browser page unreadable: {url}")
            detail = "unknown" if exc.error is None else f"{type(exc.error).__name__}: {exc.error}"
            logger.error("browser page unreadable", url=url, error=detail)
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"browser page unreadable: {detail}")
        except _ChallengeUnresolved:
            return None, RequestFailure(
                kind=FailureKind.UNEXPECTED,
                message=f"cloudflare challenge not resolved: {url}",
                reason=FailureReason.CLOUDFLARE_CHALLENGE,
            )
        except Exception as exc:
            if _is_timeout(exc):
                return None, RequestFailure(kind=FailureKind.TIMEOUT, message=f"browser timeout: {url}")
            logger.error("browser page fetch failed", url=url, error=str(exc))
            return None, RequestFailure(
                kind=FailureKind.UNEXPECTED,
                message=f"browser error: {type(exc).__name__}: {exc}",
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.debug("browser page close failed (ignored)", url=url, error=str(exc))

    async def _idle_content(self, page: _PageLike, settled: str, timeout_ms: float, deadline: float) -> str:
        """等网络静默到上限再读正文; 挑战页与长轮询不会 idle, 到点按当前正文继续.

        ``wait_for_load_state`` 判的是当前状态, 已静默立即返回, 所以放在挑战消解之后不会空等.
        静默期间可能发生导航, 重读撞上框架重建时沿用已消解挑战的正文, 不把整次渲染判死.
        """
        budget = min(timeout_ms, _remaining_ms(deadline))
        if budget > 0:
            try:
                await page.wait_for_load_state("networkidle", timeout=budget)
            except Exception as exc:
                if _is_timeout(exc):
                    logger.debug("network idle wait timed out", timeout_ms=budget)
                else:
                    logger.warning("network idle wait failed", error=str(exc))
        try:
            return await page.content()
        except Exception as exc:
            logger.debug("content reread after idle failed; keep settled content", error=str(exc))
            return settled

    async def _settle_challenge(self, page: _PageLike, deadline: float) -> str:
        """轮询正文直到挑战消解; 超时抛 ``_ChallengeUnresolved`` 或 ``_PageUnreadable``.

        导航瞬间读正文可能撞上框架重建; 读失败不视为空页, 计入等待直到超时.
        整个等待期一次都没读到正文时抛 ``_PageUnreadable``, 页面崩溃不误报为挑战未解决.
        """
        read_ok = False
        last_error: BaseException | None = None
        while True:
            try:
                content: str | None = await page.content()
            except Exception as exc:
                content = None
                last_error = exc
            else:
                read_ok = True
            if content is not None and classify_block(content) is not FailureReason.CLOUDFLARE_CHALLENGE:
                return content
            if time.monotonic() >= deadline:
                if not read_ok:
                    raise _PageUnreadable(last_error)
                raise _ChallengeUnresolved
            await asyncio.sleep(_CHALLENGE_POLL_S)

    async def _ensure_browser(self) -> _BrowserLike:
        browser = self._browser
        if browser is not None:
            return browser
        async with self._launch_lock:
            if self._closed:
                raise _BackendClosed
            browser = self._browser
            if browser is None:
                stack = AsyncExitStack()
                try:
                    browser = await self._launch(stack)
                except BaseException:
                    await stack.aclose()
                    raise
                self._browser = browser
                self._stack = stack
        return browser

    async def _context(self, scope: str) -> _ContextLike:
        context = self._contexts.get(scope)
        if context is None:
            browser = await self._ensure_browser()
            context = await browser.new_context()
            self._contexts[scope] = context
        return context

    def _schedule_idle(self) -> None:
        if self._idle_timeout <= 0:
            return
        self._idle_gen += 1
        self._idle_task = asyncio.create_task(self._idle_close(self._idle_gen))

    async def _idle_close(self, gen: int) -> None:
        """代际替代取消: 已有请求时重新计时, 关闭途中被越过不会中断退出清理."""
        await asyncio.sleep(self._idle_timeout)
        if gen != self._idle_gen:
            return
        if self._active:
            self._schedule_idle()
            return
        await self._close_browser()

    async def _close_browser(self, *, force: bool = False) -> None:
        async with self._launch_lock:
            if self._active and not force:
                return
            self._contexts.clear()
            stack, self._stack = self._stack, None
            self._browser = None
            if stack is not None:
                await stack.aclose()

    async def close(self) -> None:
        self._closed = True
        self._idle_gen += 1
        task, self._idle_task = self._idle_task, None
        await self._close_browser(force=True)
        # 释放完成后取消残留的休眠任务; cleanup 只在锁内进行, 此刻不可能被打断.
        if task is not None and not task.done():
            task.cancel()


class PatchrightBackend(_LocalBackend):
    """patchright 驱动的系统 Chrome; ``AMANE_SHOW_BROWSER`` 置位时强制有头."""

    async def _launch(self, stack: AsyncExitStack) -> _BrowserLike:
        from patchright.async_api import async_playwright

        playwright = await stack.enter_async_context(async_playwright())
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=os.getenv("AMANE_SHOW_BROWSER") is None,
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": self._proxy} if self._proxy else None,
        )
        stack.push_async_callback(browser.close)
        return browser


class _CamoufoxOptions(TypedDict):
    """``AsyncCamoufox`` 的启动参数."""

    headless: bool
    humanize: bool
    locale: str
    proxy: NotRequired[dict[str, str]]


class CamoufoxBackend(_LocalBackend):
    """stealth Firefox; 浏览器二进制在首次启动时下载."""

    async def _launch(self, stack: AsyncExitStack) -> _BrowserLike:
        from camoufox.async_api import AsyncCamoufox
        from playwright.async_api import Browser

        options: _CamoufoxOptions = {
            "headless": os.getenv("AMANE_SHOW_BROWSER") is None,
            "humanize": True,
            "locale": "ja-JP",
        }
        if self._proxy:
            options["proxy"] = {"server": self._proxy}
        browser = await stack.enter_async_context(AsyncCamoufox(**options))
        # 未启用 persistent_context, 返回值必为 Browser.
        if not isinstance(browser, Browser):
            raise RuntimeError("camoufox persistent context is not supported")
        return browser


class SolverBackend:
    """FlareSolverr 兼容服务: HTML 取自 ``solution.response``, 会话按来源复用且同会话串行.

    只支持 ``request.get``; 请求头与选择器等待由 solver 自行决定,
    因此 ``headers`` / ``wait_for`` 不参与转发 (本地后端支持).
    """

    def __init__(
        self,
        *,
        web_provider: Callable[[], WebClient],
        url: str,
        default_timeout: float,
        proxy: str | None = None,
    ) -> None:
        self._web_provider = web_provider
        self._url = url.rstrip("/")
        self._default_timeout = default_timeout
        self._proxy = _solver_proxy(proxy)
        self._scope_sessions: dict[str, str] = {}
        # 远端名称不能直接使用来源 ID: 多实例和热重建必须各自持有、销毁自己的会话.
        self._sessions: set[str] = set()
        self._destroying_sessions: set[str] = set()
        self._session_cookies: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
        self._session_cookie_config: dict[str, tuple[tuple[str, str], ...]] = {}
        # 有些兼容服务 (见 trawl) 只在**无 session** 时响应, 会话绑定的请求会挂到超时:
        # 首次踩到就把该后端标记为不支持会话, 之后一律走一次性请求 (实例随热重建重置).
        self._sessionless = False
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
        network_idle_timeout: float | None = None,
    ) -> BrowserPageResult:
        if self._closed:
            return None, self._closed_error().failure
        web = self._web_provider()
        with web.lease():
            return await self._get_page(
                web,
                url,
                scope=scope,
                cookies=cookies,
                wait_for=wait_for,
                timeout=timeout,
                network_idle_timeout=network_idle_timeout,
            )

    async def _get_page(
        self,
        web: WebClient,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None,
        wait_for: str | None,
        timeout: float | None,
        network_idle_timeout: float | None,
    ) -> BrowserPageResult:
        """取页全程使用同一通道, 包含会话创建、排队与降级重试之间的等待."""
        if wait_for is not None or network_idle_timeout is not None:
            logger.debug("solver ignores wait_for and network idle timeout", scope=scope)
        effective = timeout if timeout is not None else self._default_timeout
        async with self._semaphore, self._lock_for(scope):
            try:
                data = await self._request(web, url, scope, effective, cookies)
            except RequestError as exc:
                return None, exc.failure or RequestFailure(kind=FailureKind.UNEXPECTED, message=exc.message)
            if not _solver_ok(data):
                message = _solver_message(data)
                if _is_missing_session(message) and not self._sessionless:
                    self._forget_session(scope)
                    try:
                        data = await self._request(web, url, scope, effective, cookies)
                    except RequestError as exc:
                        return None, exc.failure or RequestFailure(kind=FailureKind.UNEXPECTED, message=exc.message)
                    if not _solver_ok(data):
                        return None, _solver_failure(_solver_message(data))
                else:
                    return None, _solver_failure(message)

            solution = data.get("solution")
            if not isinstance(solution, dict):
                return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="solver response has no solution")
            html = solution.get("response")
            if not isinstance(html, str):
                return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="solver solution has no response")
            status = solution.get("status")
            if isinstance(status, int) and status >= 400:
                return None, RequestFailure(
                    kind=FailureKind.HTTP_STATUS,
                    status=status,
                    message=f"solver target HTTP {status}",
                    body=html.encode("utf-8")[:65536],
                )
            return html, None

    def _lock_for(self, scope: str) -> asyncio.Lock:
        """会话级互斥: 一个会话只有一个浏览器标签, 并发请求会互相读到对方导航的正文."""
        lock = self._scope_locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self._scope_locks[scope] = lock
        return lock

    async def _request(
        self, web: WebClient, url: str, scope: str, timeout_ms: float, cookies: Mapping[str, str] | None
    ) -> dict[str, Any]:
        """一次取页: 优先复用会话; 该后端不吃会话时降级为无会话请求."""
        if self._sessionless:
            return await self._request_get(web, url, None, timeout_ms, cookies)
        try:
            cookie_config = tuple(sorted((cookies or {}).items()))
            previous = self._scope_sessions.get(scope)
            if previous is not None and self._session_cookie_config.get(previous, ()) != cookie_config:
                # 空数组不能清除 Trawl 的旧身份; 配置集合变化时重新建立本来源的会话.
                self._detach_session(scope)
                await self._destroy_session(web, previous)
            session = await self._ensure_session(web, scope)
            self._session_cookie_config[session] = cookie_config
            return await self._request_get(web, url, session, timeout_ms, cookies)
        except RequestError as exc:
            # 兼容服务 (trawl 之类): sessions.create 正常返回, 但会话绑定的 request.get 会一直挂到超时,
            # 或等满 maxTimeout 后回 5xx. 只对这两类降级 —— 配置错、服务已关之类重试也没意义, 照常抛出.
            if not _session_fallback_allowed(exc.failure):
                raise
            logger.warning("solver sessions unusable, fall back to sessionless", url=self._url, error=str(exc))
            self._sessionless = True
            # 降级只停止复用, 已建立的会话仍由 close 销毁.
            return await self._request_get(web, url, None, timeout_ms, cookies)

    async def _request_get(
        self, web: WebClient, url: str, session: str | None, timeout_ms: float, cookies: Mapping[str, str] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(timeout_ms),
        }
        if self._proxy and session is None:
            # 有会话的请求只采用创建时的代理; 无会话请求按标准 FlareSolverr 对象传递.
            payload["proxy"] = self._proxy
        if session is not None:
            payload["session"] = session
        cookie_key = (urlsplit(url).hostname or "", tuple(sorted((cookies or {}).items())))
        import_cookies = bool(cookies) and (session is None or self._session_cookies.get(session) != cookie_key)
        if import_cookies:
            assert cookies is not None
            # Trawl 导入非空 cookies 会删除整个 context 的旧 cookies, 重复导入会丢失已取得的 clearance.
            if session is not None:
                self._session_cookies.pop(session, None)
            payload["cookies"] = [{"name": name, "value": value} for name, value in cookies.items()]
        try:
            data = await self._post(web, payload, timeout_s=timeout_ms / 1000 + _SOLVER_HTTP_SLACK_S)
        except RequestError as exc:
            failure = exc.failure
            message = failure.body.decode("utf-8", errors="replace") if failure and failure.body else ""
            if session is not None and failure and failure.status in {404, 410} and _is_missing_session(message):
                # Trawl 用 HTTP 状态表达会话过期; 统一交给上层的一次重建, 不重复整个请求预算.
                return {"status": "error", "message": message}
            raise
        if session is not None and import_cookies and _solver_ok(data) and not self._closed:
            self._session_cookies[session] = cookie_key
        return data

    async def _ensure_session(self, web: WebClient, scope: str) -> str:
        if self._closed:
            raise self._closed_error()
        if scope in self._scope_sessions:
            return self._scope_sessions[scope]
        async with self._lock:
            if self._closed:
                raise self._closed_error()
            if scope in self._scope_sessions:
                return self._scope_sessions[scope]
            session = f"amane-{uuid4().hex}"
            payload: dict[str, Any] = {"cmd": "sessions.create", "session": session}
            if self._proxy:
                payload["proxy"] = self._proxy
            try:
                data, cancelled = await self._session_command(web, payload)
            except asyncio.CancelledError as exc:
                # 发送后失去应答仍可能已经建立远端会话; 随机 ID 只属于本次创建.
                cause = exc.__cause__
                collision = isinstance(cause, RequestError) and _is_existing_session(
                    cause.message
                    + (
                        cause.failure.body.decode("utf-8", errors="replace")
                        if cause.failure and cause.failure.body
                        else ""
                    )
                )
                if not collision:
                    self._sessions.add(session)
                    await self._destroy_session(web, session)
                raise
            if not _solver_ok(data) or _is_existing_session(_solver_message(data)):
                # 明确冲突时不能接管或销毁其他实例的会话.
                if cancelled:
                    raise asyncio.CancelledError
                raise RequestError(self._url, _solver_failure(_solver_message(data)))
            if cancelled or self._closed:
                # 取消或 close 可能早于创建结果; 取得结果后回收, 不写回会话索引.
                self._sessions.add(session)
                await self._destroy_session(web, session)
                if cancelled:
                    raise asyncio.CancelledError
                raise self._closed_error()
            self._sessions.add(session)
            self._scope_sessions[scope] = session
            return session

    def _forget_session(self, scope: str) -> None:
        session = self._detach_session(scope)
        if session is not None:
            self._sessions.discard(session)

    def _detach_session(self, scope: str) -> str | None:
        session = self._scope_sessions.pop(scope, None)
        if session is not None:
            self._session_cookies.pop(session, None)
            self._session_cookie_config.pop(session, None)
        return session

    def _closed_error(self) -> RequestError:
        return RequestError(self._url, RequestFailure(kind=FailureKind.UNEXPECTED, message="solver backend closed"))

    async def _post(self, web: WebClient, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        # solver 响应内嵌目标页 HTML, 只记 meta 避免任务记录膨胀.
        with skip_body_recording():
            resp = await web.request(
                "POST", f"{self._url}/v1", json=payload, use_proxy=False, timeout=timeout_s, max_attempts=1
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise RequestError(
                self._url,
                RequestFailure(kind=FailureKind.UNEXPECTED, message=f"solver response is not JSON: {exc}"),
            ) from exc
        if not isinstance(data, dict):
            raise RequestError(
                self._url, RequestFailure(kind=FailureKind.UNEXPECTED, message="solver response is not an object")
            )
        return data

    async def _session_command(self, web: WebClient, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """会话命令完成后再传播取消, 防止远端创建完成而本地丢失所有权. HTTP 时限限制等待."""
        task = asyncio.create_task(self._bounded_session_post(web, payload))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception as exc:
                if cancelled:
                    raise asyncio.CancelledError from exc
                raise

    async def _bounded_session_post(self, web: WebClient, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(_SOLVER_SESSION_TIMEOUT_S):
                return await self._post(web, payload, timeout_s=_SOLVER_SESSION_TIMEOUT_S)
        except TimeoutError as exc:
            raise RequestError(
                self._url, RequestFailure(kind=FailureKind.TIMEOUT, message="solver session command timeout")
            ) from exc

    async def close(self) -> None:
        self._closed = True
        sessions = list(self._sessions)
        self._sessions.clear()
        self._scope_sessions.clear()
        self._session_cookies.clear()
        self._session_cookie_config.clear()
        if sessions:
            web = self._web_provider()
            with web.lease():
                await asyncio.gather(*(self._destroy_session(web, scope) for scope in sessions))

    async def _destroy_session(self, web: WebClient, scope: str) -> None:
        if scope in self._destroying_sessions:
            return
        self._destroying_sessions.add(scope)
        try:
            try:
                data, cancelled = await self._session_command(web, {"cmd": "sessions.destroy", "session": scope})
            except Exception as exc:
                logger.debug("solver session destroy failed (ignored)", session=scope, error=str(exc))
                return
            if _solver_ok(data) or _is_missing_session(_solver_message(data)):
                self._sessions.discard(scope)
            if cancelled:
                raise asyncio.CancelledError
        finally:
            self._destroying_sessions.discard(scope)


def _solver_proxy(value: str | None) -> dict[str, str] | None:
    """标准 FlareSolverr 使用对象; Trawl 同时接受此形态. 认证字段单独传递, 保留 IPv6 地址."""
    if not value:
        return None
    parsed = urlsplit(value)
    proxy = {"url": urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1]))}
    if parsed.username is not None or parsed.password is not None:
        proxy["username"] = unquote(parsed.username or "")
        proxy["password"] = unquote(parsed.password or "")
    return proxy


def _solver_ok(data: dict[str, Any]) -> bool:
    return data.get("status") == "ok"


def _solver_message(data: dict[str, Any]) -> str:
    message = data.get("message")
    return message if isinstance(message, str) and message else "solver error"


def _solver_failure(message: str) -> RequestFailure:
    lower = message.lower()
    if "timeout" in lower:
        return RequestFailure(kind=FailureKind.TIMEOUT, message=message)
    if "challenge" in lower:
        return RequestFailure(kind=FailureKind.UNEXPECTED, message=message, reason=FailureReason.CLOUDFLARE_CHALLENGE)
    return RequestFailure(kind=FailureKind.UNEXPECTED, message=message)


def _is_missing_session(message: str) -> bool:
    lower = message.lower()
    return "session" in lower and any(
        fragment in lower for fragment in ("not found", "does not exist", "unknown", "expired", "unavailable")
    )


def _session_fallback_allowed(failure: RequestFailure | None) -> bool:
    """会话绑定的取页失败, 是否可能由「该后端不支持会话」造成.

    trawl 这类兼容服务: ``sessions.create`` 正常返回, 但带 session 的 ``request.get`` 会一直挂到
    超时, 或等满 ``maxTimeout`` 后回 5xx. 只对这两类降级 —— 配置错、服务已关之类重试也没意义.
    """
    if failure is None:
        return False
    if failure.kind is FailureKind.TIMEOUT:
        return True
    return failure.kind is FailureKind.HTTP_STATUS and (failure.status or 0) >= 500


def _is_existing_session(message: str) -> bool:
    lower = message.lower()
    return "session" in lower and ("already" in lower or "exist" in lower)


def _remaining_ms(deadline: float) -> float:
    return max(0.0, (deadline - time.monotonic()) * 1000)


def _is_timeout(exc: BaseException) -> bool:
    """playwright 的 TimeoutError 不继承内建异常, 且可选依赖不能顶层导入, 按类名识别."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError"
