"""麦麦看本。

群里出现「禁漫本子号」（JM123 / 禁漫123 / JM123456 或纯数字）时，
自动抓取该本子的标题和作者并回帖；回帖可引用原消息、可打包成聊天记录、结果可注入上下文。

封面打码走本地 AutoHajimiMosaic ONNX 服务（二次元分割 mask，正常马赛克），
支持「兜底开关」防止漏检，支持按部位类别选择打码。

数据：标题/作者来自 jmcomic 库（自动处理域名漂移+反爬）；封面打码来自 ahm_api_url。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, ClassVar, Literal

from maibot_sdk import Command, ConfigWidget, ConfigWidgetState, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

SUPPORTED_CONFIG_VERSION = "1.0.0"


def _load_jmcomic():
    """延迟导入 jmcomic 并静默它的刷屏日志。"""
    import jmcomic  # noqa: PLC0415

    try:
        jmcomic.disable_jm_log()
    except Exception:
        pass
    return jmcomic


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用", json_schema_extra={"label": "启用", "order": 0})
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本，由插件维护",
        json_schema_extra={"hidden": True, "disabled": True, "order": 99},
    )


class JmConfig(PluginConfigBase):
    """`[jm]` 配置节（触发 → 抓取 → 回复 → 发送 → 打码/兜底）。"""

    __ui_label__ = "麦麦看本"
    __ui_icon__ = "search"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True, description="插件总开关",
        json_schema_extra={"label": "启用查询", "hint": "总开关。关闭后不再自动检测本子号，/jm 命令也不可用。", "order": 0})
    allowed_groups: list[str] = Field(
        default_factory=list, description="允许使用的群",
        json_schema_extra={"label": "启用群（留空=全部）", "hint": "分群开关：留空 = 所有群生效；填写群号（如 123456789）或会话ID，则只在这些群里触发，/jm 命令同样受限。", "order": 1})
    trigger_mode: Literal["prefix", "any"] = Field(
        default="prefix", description="触发模式",
        json_schema_extra={"label": "触发模式", "hint": "prefix：必须「前缀+数字」才触发（只扫当前消息正文，引用旧消息不算），最稳；any：消息里出现纯数字且达到位数就触发。", "order": 2})
    prefixes: list[str] = Field(
        default_factory=lambda: ["JM", "jm", "禁漫", "本子"], description="触发前缀",
        json_schema_extra={"label": "触发前缀", "hint": "仅 prefix 模式生效，大小写不敏感。如 JM、jm、禁漫、本子；发 JM123 / 禁漫123 / 本子123 都能命中。", "order": 3})
    min_digits: int = Field(
        default=4, description="最少位数",
        json_schema_extra={"label": "最少位数", "hint": "仅 any（纯数字）模式用：短于该位数的数字不当作本子号。", "order": 4})
    max_digits: int = Field(
        default=8, description="最多位数",
        json_schema_extra={"label": "最多位数", "hint": "前缀/纯数字模式下，超过该位数的数字不当作本子号。", "order": 5})
    cooldown_seconds: int = Field(
        default=60, description="冷却秒数",
        json_schema_extra={"label": "冷却(秒)", "hint": "同一个群这么多秒内只抓一次，防刷屏。填 0 = 不冷却（仅测试用）。", "order": 6})
    request_timeout: float = Field(
        default=20.0, description="超时秒数",
        json_schema_extra={"label": "超时(秒)", "hint": "抓标题 / 下载封面 / 打码的单次网络超时；网络差可调大。", "order": 7})
    send_author: bool = Field(
        default=True, description="是否显示作者",
        json_schema_extra={"label": "显示作者", "hint": "回复是否带「作者：xxx」；关闭则只回《标题》。", "order": 8})
    reply_template: str = Field(
        default="《{title}》 作者：{author}", description="回复模板",
        json_schema_extra={"label": "回复模板", "hint": "命中时的回复文案。变量：{title}=标题、{author}=作者。关闭「显示作者」时只回《标题》。", "order": 9})
    on_not_found: Literal["silent", "reply"] = Field(
        default="silent", description="无结果处理",
        json_schema_extra={"label": "无结果处理", "hint": "没搜到时的行为：silent=不回复；reply=回一句（内容见「无结果文案」）。", "order": 10})
    not_found_text: str = Field(
        default="没搜到这个本子", description="无结果文案",
        json_schema_extra={"label": "无结果文案", "hint": "仅当「无结果处理=reply」时使用：没搜到时回复的内容。", "order": 11})
    send_cover: bool = Field(
        default=False, description="是否发送封面",
        json_schema_extra={"label": "发送封面", "hint": "是否附带封面图。开启后封面会先送本地打码服务处理再发；关闭则只发文字。", "order": 12})
    bundle_forward: bool = Field(
        default=True, description="分开发/打包聊天记录",
        json_schema_extra={"label": "打包成聊天记录（关=分开发）", "hint": "开：标题+封面合成一条合并转发（聊天记录），更整洁；关：标题与封面分两条发。⚠️合并转发无法引用原消息；仅「发送封面」开启时才有区别。", "order": 13})
    quote_original: bool = Field(
        default=True, description="是否引用原消息",
        json_schema_extra={"label": "引用原消息", "hint": "分开发模式下，回复引用触发者那条原消息（QQ 引用气泡）。打包成聊天记录时自动忽略。", "order": 14})
    inject_context: bool = Field(
        default=True, description="是否注入上下文",
        json_schema_extra={"label": "注入上下文", "hint": "把查询结果写入麦麦的聊天上下文，之后被追问也能接话；关闭则只是发条消息、不记忆。", "order": 15})
    masking_enabled: bool = Field(
        default=True, description="是否启用打码服务（是否安装/使用打码）",
        json_schema_extra={"label": "启用打码服务", "hint": "让用户自己选择是否使用打码：开=封面会送下方 API 打码后发出（需先部署好打码服务）；关=不使用打码、不发封面（只发标题）。", "order": 16})
    ahm_api_url: str = Field(
        default="http://127.0.0.1:8000/process/", description="打码服务 API 地址",
        json_schema_extra={"label": "打码 API 地址", "hint": "本地打码服务接口（默认 http://127.0.0.1:8000/process/）。部署方式见 README / install.sh。", "order": 17})
    mosaic_style: Literal["mosaic", "blur"] = Field(
        default="mosaic", description="打码样式",
        json_schema_extra={"label": "打码样式", "hint": "封面打码外观：mosaic=像素马赛克；blur=高斯模糊。", "order": 18})
    fallback_enabled: bool = Field(
        default=True, description="漏检兜底总开关",
        json_schema_extra={"label": "启用漏检兜底", "hint": "兜底总开关。开：判定为敏感但一个部位都没检出时，按「兜底方式」处理，防止模型漏检导致露点；关：不兜底（漏检就会露）。", "order": 19})
    fallback: Literal["mosaic", "blur", "skip"] = Field(
        default="mosaic", description="兜底方式",
        json_schema_extra={"label": "兜底方式", "hint": "触发兜底时：mosaic=整图马赛克；blur=整图模糊；skip=不发封面只发标题。仅「启用漏检兜底」开启时生效。", "order": 20})
    fallback_cover_ratio: float = Field(
        default=0.0, description="兜底覆盖阈值",
        json_schema_extra={"label": "兜底覆盖阈值", "hint": "0 = 关闭（推荐，只按「一个部位都没检出」兜底，避免正常小露点被误整图糊）；设 >0 则检出覆盖率低于它也会兜底（更谨慎，但正常图也可能被整图糊）。", "order": 21})
    min_boxes: int = Field(
        default=1, description="兜底最少框数",
        json_schema_extra={"label": "兜底最少框数", "hint": "检出框数少于它视为「过少」→ 触发兜底。默认 1 = 一个部位都没检出才兜底。", "order": 22})
    proxy_enabled: bool = Field(
        default=False, description="允许使用代理服务",
        json_schema_extra={"label": "允许使用代理服务", "hint": "开启后，jmcomic 抓取（查询/下载封面）走下方代理；打码 API 是本地直连，不受影响。", "order": 23})
    proxy_url: str = Field(
        default="", description="代理地址",
        json_schema_extra={"label": "代理地址", "hint": "如 http://127.0.0.1:7890 或 socks5://127.0.0.1:7890；仅「允许使用代理服务」开启时生效。", "order": 24})
    jm_domain: str = Field(
        default="", description="指定 jmcomic 分流（留空=自动）",
        json_schema_extra={"label": "指定分流（留空=自动）", "hint": "填 API 分流域名（如 www.cdngwc.cc）。可先用页面下方「检测 jmcomic 分流」看各分流延迟/访问，再选最快的填这里；留空=自动。", "order": 25})


class RootConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    jm: JmConfig = Field(default_factory=JmConfig)


class JmLookupPlugin(MaiBotPlugin):
    config_model = RootConfig
    config_widgets: ClassVar[list[ConfigWidget]] = [
        ConfigWidget(id="check_api", kind="action", label="检测打码 API"),
        ConfigWidget(id="api_status", kind="status", label="打码 API 状态"),
        ConfigWidget(id="check_proxy", kind="action", label="测试代理（代理设置）"),
        ConfigWidget(id="proxy_status", kind="status", label="代理状态"),
        ConfigWidget(id="check_domains", kind="action", label="检测 jmcomic 分流"),
        ConfigWidget(id="domains_status", kind="status", label="jmcomic 分流（延迟 / 访问）"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._states: dict[str, ConfigWidgetState] = {
            "api_status": ConfigWidgetState(text="未检测。点击「检测打码 API」。", tone="neutral"),
            "proxy_status": ConfigWidgetState(text="未检测。点击「测试代理（代理设置）」。", tone="neutral"),
            "domains_status": ConfigWidgetState(text="未检测。点击「检测 jmcomic 分流」。", tone="neutral"),
        }
        self._cache: dict[int, tuple[str, str] | None] = {}
        self._last: dict[str, float] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._client: Any = None
        self._client_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期
    async def on_load(self) -> None:
        self.ctx.logger.info("禁漫号查询插件已加载")

    async def on_unload(self) -> None:
        self._cache.clear()
        self.ctx.logger.info("禁漫号查询插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("禁漫号查询插件配置已更新: version=%s", version)

    async def get_config_widget_state(self) -> dict[str, ConfigWidgetState]:
        return {
            "check_api": ConfigWidgetState(),
            "api_status": self._states["api_status"],
            "check_proxy": ConfigWidgetState(),
            "proxy_status": self._states["proxy_status"],
            "check_domains": ConfigWidgetState(),
            "domains_status": self._states["domains_status"],
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id == "check_api":
            self._states["api_status"] = await self._check_api()
        elif action_id == "check_proxy":
            self._states["proxy_status"] = await self._check_proxy()
        elif action_id == "check_domains":
            self._states["domains_status"] = await self._check_domains()
        else:
            raise ValueError(f"未知配置页操作: {action_id}")
        self.ctx.logger.info("[jm] 配置页检测 %s 完成", action_id)

    # ---------------------------------------------------------------- 触发解析
    def _extract_ids(self, text: str, cfg: JmConfig) -> list[str]:
        if cfg.trigger_mode == "any":
            lo = max(1, int(cfg.min_digits)); hi = max(lo, int(cfg.max_digits))
            return list(dict.fromkeys(re.compile(rf"(?<!\d)(\d{{{lo},{hi}}})(?!\d)").findall(text)))
        prefixes = [str(p).strip() for p in (cfg.prefixes or []) if str(p).strip()]
        if not prefixes:
            return []
        alts = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
        hi = max(1, int(cfg.max_digits))
        return list(dict.fromkeys(re.compile(rf"(?:{alts})\s*[号:：#\-]?\s*(\d{{1,{hi}}})", re.IGNORECASE).findall(text)))

    # ---------------------------------------------------------------- jmcomic 抓取
    async def _get_client(self):
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                def _mk():
                    jm = _load_jmcomic()
                    cfg = self.config.jm
                    try:
                        if getattr(cfg, "proxy_enabled", False) and str(getattr(cfg, "proxy_url", "")).strip():
                            jm.JmModuleConfig.DEFAULT_PROXIES = jm.ProxyBuilder.build_by_str(str(cfg.proxy_url).strip())
                        else:
                            jm.JmModuleConfig.DEFAULT_PROXIES = {}
                    except Exception:
                        pass
                    option = jm.JmOption.default()
                    domain = str(getattr(cfg, "jm_domain", "") or "").strip()
                    if domain:
                        try:
                            option.client["domain"] = [domain]
                        except Exception:
                            pass
                    return option.new_jm_client()
                self._client = await asyncio.to_thread(_mk)
        return self._client

    async def _lookup(self, aid: int, timeout: float) -> tuple[str, str] | None:
        if aid in self._cache:
            return self._cache[aid]
        res: tuple[str, str] | None = None
        try:
            client = await asyncio.wait_for(self._get_client(), timeout=timeout)

            def _fetch():
                album = client.get_album_detail(aid)
                name = str(getattr(album, "name", "") or "").strip()
                author = str(getattr(album, "author", "") or "").strip()
                if not author:
                    authors = getattr(album, "authors", None)
                    if isinstance(authors, (list, tuple)) and authors:
                        author = str(authors[0]).strip()
                return (name, author) if name else None

            async with self._fetch_lock:
                res = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=timeout)
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 查询 {aid} 失败: {type(exc).__name__}: {exc}")
            res = None
        self._cache[aid] = res
        return res

    # ---------------------------------------------------------------- 组装/发送
    def _build_text(self, res: tuple[str, str], cfg: JmConfig) -> str:
        name, author = res
        if cfg.send_author and author:
            try:
                return cfg.reply_template.format(title=name, author=author)
            except (KeyError, ValueError):
                return f"《{name}》 作者：{author}"
        return f"《{name}》"

    def _build_segments(self, text: str, reply_ctx: dict[str, Any], cfg: JmConfig) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        mid = str(reply_ctx.get("message_id") or "").strip()
        if cfg.quote_original and mid:
            data: dict[str, Any] = {"target_message_id": mid, "target_message_content": str(reply_ctx.get("content") or "")}
            for key in ("sender_id", "sender_nickname", "sender_cardname"):
                if reply_ctx.get(key):
                    data["target_message_" + key] = reply_ctx[key]
            segments.append({"type": "reply", "data": data})
        segments.append({"type": "text", "data": text})
        return segments

    async def _send(self, stream_id: str, text: str, reply_ctx: dict[str, Any]) -> bool:
        cfg = self.config.jm
        segments = self._build_segments(text, reply_ctx, cfg)
        extra = dict(sync_to_maisaka_history=bool(cfg.inject_context), maisaka_source_kind="jm_lookup")
        try:
            if len(segments) > 1:
                ok = await self.ctx.send.hybrid(segments, stream_id, **extra)
                if ok:
                    return True
            return bool(await self.ctx.send.text(text, stream_id, **extra))
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 发送失败: {type(exc).__name__}: {exc}")
            try:
                return bool(await self.ctx.send.text(text, stream_id))
            except Exception:
                return False

    async def _send_image(self, stream_id: str, b64: str) -> None:
        cfg = self.config.jm
        try:
            await self.ctx.send.image(b64, stream_id, sync_to_maisaka_history=bool(cfg.inject_context), maisaka_source_kind="jm_lookup")
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 发送封面失败: {type(exc).__name__}: {exc}")

    async def _send_forward(self, stream_id: str, text: str, cover_b64: str) -> None:
        cfg = self.config.jm
        nodes = [
            {"user_id": "", "nickname": "本子信息", "content": [{"type": "text", "content": text}]},
            {"user_id": "", "nickname": "封面", "content": [{"type": "image", "content": cover_b64}]},
        ]
        try:
            if await self.ctx.send.forward(nodes, stream_id, sync_to_maisaka_history=bool(cfg.inject_context), maisaka_source_kind="jm_lookup"):
                return
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 合并转发失败，回退分开发送: {type(exc).__name__}: {exc}")
        await self._send(stream_id, text, {})
        await self._send_image(stream_id, cover_b64)

    async def _handle(self, aid: int, stream_id: str, reply_ctx: dict[str, Any]) -> None:
        cfg = self.config.jm
        res = await self._lookup(aid, float(cfg.request_timeout))
        if res is None:
            if cfg.on_not_found == "reply":
                await self._send(stream_id, cfg.not_found_text or "没搜到这个本子", reply_ctx)
            return
        text = self._build_text(res, cfg)
        if not cfg.send_cover:
            await self._send(stream_id, text, reply_ctx)
            return
        cover_b64 = await self._prepare_cover(aid)
        if cfg.bundle_forward and cover_b64:
            await self._send_forward(stream_id, text, cover_b64)
        else:
            await self._send(stream_id, text, reply_ctx)
            if cover_b64:
                await self._send_image(stream_id, cover_b64)

    # ---------------------------------------------------------------- 封面打码（AHM ONNX 服务）
    def _cover_path(self, aid: int) -> str:
        import os  # noqa: PLC0415

        try:
            base = str(self.ctx.paths.runtime_dir)
        except Exception:
            base = ""
        d = base or "/tmp"
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = "/tmp"
        return os.path.join(d, f"jm_cover_{aid}.jpg")

    async def _download_cover(self, aid: int, timeout: float):
        client = await asyncio.wait_for(self._get_client(), timeout=timeout)
        path = self._cover_path(aid)

        def _dl():
            import os  # noqa: PLC0415

            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            client.download_album_cover(aid, path)
            return path if os.path.exists(path) and os.path.getsize(path) > 0 else None

        try:
            return await asyncio.wait_for(asyncio.to_thread(_dl), timeout=timeout)
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 下载封面 {aid} 失败: {type(exc).__name__}: {exc}")
            return None

    async def _ahm_process(self, path: str) -> str | None:
        """调 AHM ONNX 服务打码；返回打码图 base64 / 'SKIP'(不发) / None(失败)。"""
        cfg = self.config.jm
        try:
            import base64 as _b64  # noqa: PLC0415

            import httpx  # noqa: PLC0415

            with open(path, "rb") as f:
                data = f.read()
            fallback = str(cfg.fallback) if cfg.fallback_enabled else "off"
            async with httpx.AsyncClient(timeout=float(cfg.request_timeout)) as cli:
                r = await cli.post(
                    str(cfg.ahm_api_url),
                    params={
                        "output_format": "jpg",
                        "style": str(cfg.mosaic_style),
                        "fallback": fallback,
                        "fallback_cover_ratio": float(cfg.fallback_cover_ratio),
                        "min_boxes": int(cfg.min_boxes),
                    },
                    files={"file": ("cover.jpg", data, "image/jpeg")},
                )
            if r.headers.get("x-skip") == "1":
                self.ctx.logger.info("[jm] 兜底=skip：判定敏感但检出过少，本次不发封面")
                return "SKIP"
            if r.status_code == 200 and r.content:
                self.ctx.logger.info(f"[jm] 打码成功 category={r.headers.get('x-category','')} fallback={r.headers.get('x-fallback','0')}")
                return _b64.b64encode(r.content).decode()
            self.ctx.logger.warning(f"[jm] 打码服务返回异常 status={r.status_code}")
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 打码服务不可用，本次不发封面: {type(exc).__name__}: {exc}")
        return None

    async def _prepare_cover(self, aid: int) -> str | None:
        cfg = self.config.jm
        if not cfg.masking_enabled:
            return None  # 用户选择不使用打码：不发封面（只发标题）
        path = await self._download_cover(aid, float(cfg.request_timeout))
        if not path:
            return None
        b64 = await self._ahm_process(path)
        if b64 == "SKIP":
            return None
        return b64  # 成功=base64；失败=None（不发封面，仅标题）

    # ---------------------------------------------------------------- 检测（按钮 / 命令共用）
    async def _probe_url(self, url: str, timeout: float, via_proxy: str = "") -> tuple[Any, Any, Any]:
        """单次探测，返回 (状态码, 耗时ms, 异常名)。"""
        import httpx  # noqa: PLC0415

        t0 = time.monotonic()
        try:
            kw: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
            if via_proxy:
                kw["proxy"] = via_proxy
            async with httpx.AsyncClient(**kw) as cli:
                r = await cli.get(url)
            return (r.status_code, int((time.monotonic() - t0) * 1000), None)
        except Exception as exc:
            return (None, None, type(exc).__name__)

    async def _check_api(self) -> ConfigWidgetState:
        cfg = self.config.jm
        api_url = str(cfg.ahm_api_url or "").strip()
        docs_url = api_url.replace("/process/", "/docs") if "/process/" in api_url else api_url.rstrip("/") + "/docs"
        code, ms, err = await self._probe_url(docs_url, 3.0)
        if code is None:
            return ConfigWidgetState(text=f"打码 API：❌ 不可达（{err}）\n{docs_url}", tone="error")
        ok = code < 500
        return ConfigWidgetState(text=f"打码 API：{'✅ 可用' if ok else '❌ 异常'} HTTP {code}（{ms}ms）\n{docs_url}",
                                 tone="success" if ok else "warning")

    async def _check_proxy(self) -> ConfigWidgetState:
        cfg = self.config.jm
        proxy_url = str(getattr(cfg, "proxy_url", "") or "").strip()
        if not getattr(cfg, "proxy_enabled", False):
            return ConfigWidgetState(text="代理：未启用（直连）", tone="neutral")
        if not proxy_url:
            return ConfigWidgetState(text="代理：⚠️ 已开启但未填地址", tone="warning")
        code, ms, err = await self._probe_url("https://www.gstatic.com/generate_204", 4.0, proxy_url)
        if code is None:
            return ConfigWidgetState(text=f"代理：❌ 不可用（{err}）\n{proxy_url}", tone="error")
        ok = code in (200, 204)
        return ConfigWidgetState(text=f"代理：{'✅ 可用' if ok else '❌ 异常'} HTTP {code}（{ms}ms）\n{proxy_url}",
                                 tone="success" if ok else "warning")

    async def _check_domains(self) -> ConfigWidgetState:
        """并发测试 jmcomic 全部 API + HTML 分流，逐个列延迟/访问情况。"""
        cfg = self.config.jm
        proxy_url = str(getattr(cfg, "proxy_url", "") or "").strip()
        via_proxy = proxy_url if getattr(cfg, "proxy_enabled", False) and proxy_url else ""
        try:
            from jmcomic import JmModuleConfig  # noqa: PLC0415

            api_domains = list(getattr(JmModuleConfig, "DOMAIN_API_LIST", []) or [])
            updated = list(getattr(JmModuleConfig, "DOMAIN_API_UPDATED_LIST", None) or [])
            if updated:
                api_domains = list(dict.fromkeys(api_domains + updated))
            try:
                html_domains = [d for d in (JmModuleConfig.get_html_domain_all() or []) if d and "t.me" not in d]
            except Exception:
                html_domains = []
        except Exception as exc:
            return ConfigWidgetState(text=f"jmcomic 分流检测失败：{type(exc).__name__}: {exc}", tone="error")
        targets = [("API", d) for d in api_domains] + [("HTML", d) for d in html_domains]
        probes = []
        for kind, d in targets:
            url = d if str(d).startswith("http") else f"https://{d}/"
            probes.append(self._probe_url(url, 4.0, via_proxy))
        results = await asyncio.gather(*probes, return_exceptions=True)
        rows: list[tuple[str, str, Any, Any, Any]] = []
        for (kind, d), res in zip(targets, results):
            if isinstance(res, tuple):
                rows.append((kind, d, res[0], res[1], res[2]))
            else:
                rows.append((kind, d, None, None, type(res).__name__))
        ok_n = sum(1 for _, _, code, _, _ in rows if isinstance(code, int) and code < 500)
        lines = [f"jmcomic 分流：{ok_n}/{len(rows)} 可用"]
        for kind, d, code, ms, err in rows:
            if isinstance(code, int) and code < 500:
                lines.append(f"  ✅ [{kind}] {d}  HTTP {code}  {ms}ms")
            else:
                lines.append(f"  ❌ [{kind}] {d}  {'超时/失败' if code is None else 'HTTP ' + str(code)}")
        tone = "error" if ok_n == 0 else ("warning" if ok_n < len(rows) else "success")
        return ConfigWidgetState(text="\n".join(lines)[:1999], tone=tone)

    async def _run_checks(self) -> ConfigWidgetState:
        """命令 /jmcheck 用：三项并发检测后合并。"""
        api_st, proxy_st, domain_st = await asyncio.gather(self._check_api(), self._check_proxy(), self._check_domains())
        self._states["api_status"] = api_st
        self._states["proxy_status"] = proxy_st
        self._states["domains_status"] = domain_st
        tone = "error" if any(x.tone == "error" for x in (api_st, proxy_st, domain_st)) else (
            "warning" if any(x.tone == "warning" for x in (api_st, proxy_st, domain_st)) else "success")
        return ConfigWidgetState(text="\n".join(x.text for x in (api_st, proxy_st, domain_st))[:1999], tone=tone)

    # ---------------------------------------------------------------- 命令/监听
    @staticmethod
    def _current_text(message: dict[str, Any]) -> str:
        """只取当前消息自身正文，排除引用原文（避免旧 jm 号误触发）。"""
        raw = message.get("raw_message")
        if isinstance(raw, list):
            txt = "".join(str(c.get("data") or "") for c in raw if isinstance(c, dict) and str(c.get("type")) == "text").strip()
            if txt:
                return txt
        return str(message.get("processed_plain_text") or message.get("plain_text") or "").strip()

    @staticmethod
    def _group_of(message: dict[str, Any]) -> str:
        info = message.get("message_info") or {}
        if isinstance(info, dict):
            gi = info.get("group_info") or {}
            if isinstance(gi, dict) and gi.get("group_id"):
                return str(gi.get("group_id"))
        return str(message.get("group_id") or "")

    def _group_allowed(self, cfg: JmConfig, message: dict[str, Any]) -> bool:
        groups = [str(g).strip() for g in (cfg.allowed_groups or []) if str(g).strip()]
        if not groups:
            return True
        gid = self._group_of(message)
        sid = str(message.get("session_id") or "")
        return (bool(gid) and gid in groups) or (bool(sid) and sid in groups)

    @staticmethod
    def _reply_ctx_from_message(message: dict[str, Any]) -> dict[str, Any]:
        ctx: dict[str, Any] = {"message_id": str(message.get("message_id") or ""), "content": str(message.get("processed_plain_text") or "")}
        info = message.get("message_info") or {}
        ui = (info.get("user_info") if isinstance(info, dict) else None) or {}
        if isinstance(ui, dict):
            ctx["sender_id"] = str(ui.get("user_id") or "")
            ctx["sender_nickname"] = str(ui.get("user_nickname") or ui.get("nickname") or "")
            ctx["sender_cardname"] = str(ui.get("user_cardname") or ui.get("cardname") or "")
        return ctx

    @Command("jmcheck", pattern=r"^/jmcheck$")
    async def handle_jmcheck(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "").strip()
        st = await self._run_checks()
        if stream_id:
            try:
                await self.ctx.send.text(st.text, stream_id)
            except Exception:
                pass
        return True, st.text, 1

    @Command("jm", pattern=r"^/(?:jm|查本子|禁漫)\s+(?P<id>\d{2,8})$")
    async def handle_jm_command(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "").strip()
        raw = str((kwargs.get("matched_groups") or {}).get("id") or "").strip()
        if not raw or not stream_id:
            return False, "用法：/jm 123456", 1
        cfg = self.config.jm
        groups = [str(g).strip() for g in (cfg.allowed_groups or []) if str(g).strip()]
        if groups:
            msg = kwargs.get("message") or {}
            gid = self._group_of(msg) if isinstance(msg, dict) else ""
            if not ((gid and gid in groups) or (stream_id in groups)):
                return False, "本群未启用该功能", 1
        reply_ctx: dict[str, Any] = {}
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            reply_ctx = self._reply_ctx_from_message(msg)
        elif kwargs.get("message_id"):
            reply_ctx = {"message_id": str(kwargs.get("message_id")), "content": ""}
        res = await self._lookup(int(raw), float(cfg.request_timeout))
        text = (cfg.not_found_text or "没搜到这个本子") if res is None else self._build_text(res, cfg)
        if res is not None and cfg.send_cover:
            cover_b64 = await self._prepare_cover(int(raw))
            if cfg.bundle_forward and cover_b64:
                await self._send_forward(stream_id, text, cover_b64)
            else:
                await self._send(stream_id, text, reply_ctx)
                if cover_b64:
                    await self._send_image(stream_id, cover_b64)
        else:
            await self._send(stream_id, text, reply_ctx)
        return True, text, 1

    @HookHandler("chat.receive.after_process", name="jm_lookup_detect", description="检测禁漫本子号并查询标题",
                 mode=HookMode.OBSERVE, order=HookOrder.NORMAL, timeout_ms=800, error_policy=ErrorPolicy.SKIP)
    async def on_message(self, **kwargs):
        cfg = self.config.jm
        if not self.config.plugin.enabled or not cfg.enabled:
            return {}
        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            return {}
        stream_id = str(message.get("session_id") or "").strip()
        if not stream_id or not self._group_allowed(cfg, message):
            return {}
        text = self._current_text(message)
        if not text:
            return {}
        ids = self._extract_ids(text, cfg)
        if not ids:
            return {}
        now = time.monotonic()
        if cfg.cooldown_seconds > 0 and now - self._last.get(stream_id, 0.0) < cfg.cooldown_seconds:
            return {}
        self._last[stream_id] = now
        task = asyncio.create_task(self._handle(int(ids[0]), stream_id, self._reply_ctx_from_message(message)))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {}


def create_plugin() -> JmLookupPlugin:
    return JmLookupPlugin()
