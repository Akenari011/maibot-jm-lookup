"""麦麦漫画。

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
from typing import Any, ClassVar, Literal, NamedTuple

from maibot_sdk import API, Command, ConfigWidgetState, Field, HookHandler, MaiBotPlugin, PluginConfigBase
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


# ---------------------------------------------------------------- 多渠道 provider
class LookupResult(NamedTuple):
    """各渠道统一的查询结果。"""

    title: str
    author: str = ""
    url: str = ""
    cover_url: str = ""
    desc: str = ""


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _http_get(
    url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    proxy: str = "",
) -> bytes:
    """同步 GET，调用方负责丢进线程。失败抛异常，由 provider 统一兜。

    `proxy` 支持 http:// / https:// / socks5://（socks 需要装 PySocks）。
    """
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _norm_title(text: str) -> str:
    import html as _html

    return " ".join(_html.unescape(text or "").split())


class NhentaiProvider:
    """nhentai.net：公开 v2 API，无需登录。

    端点（实测可用）：
      GET /api/v2/galleries/<id>       → 详情，含 title / cover / tags(artist)
      GET /api/v2/search?query=<q>     → 列表，支持 `tag:xxx` 与 `artist:xxx` 语法
    封面走 t.nhentai.net，路径来自详情里的 cover.path。
    """

    prefix = "nh"
    BASE = "https://nhentai.net"
    CDN = "https://t.nhentai.net"

    def __init__(self, proxy: str = "") -> None:
        self.proxy = (proxy or "").strip()

    async def detail(self, gid: str, timeout: float) -> LookupResult | None:
        import json

        def _f() -> LookupResult | None:
            raw = _http_get(f"{self.BASE}/api/v2/galleries/{gid}", timeout, proxy=self.proxy)
            d = json.loads(raw)
            if not isinstance(d, dict):
                return None
            t = d.get("title") or {}
            title = _norm_title(
                str(t.get("english") or t.get("japanese") or t.get("pretty") or "")
            )
            if not title:
                return None
            author = ""
            for tag in d.get("tags") or []:
                if isinstance(tag, dict) and tag.get("type") == "artist":
                    author = str(tag.get("name") or "").strip()
                    if author:
                        break
            cover = str((d.get("cover") or {}).get("path") or "")
            return LookupResult(
                title=title,
                author=author,
                url=f"{self.BASE}/g/{gid}/",
                cover_url=f"{self.CDN}/{cover}" if cover else "",
            )

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return None

    async def search(self, query: str, limit: int, timeout: float) -> list[LookupResult]:
        """query 可以是普通关键词，也可以是 `tag:xxx` / `artist:xxx`。"""
        import json
        from urllib.parse import quote

        def _f() -> list[LookupResult]:
            raw = _http_get(f"{self.BASE}/api/v2/search?query={quote(query)}", timeout, proxy=self.proxy)
            d = json.loads(raw)
            out: list[LookupResult] = []
            for item in (d.get("result") or [])[: max(1, min(10, limit))]:
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("id") or "")
                title = _norm_title(
                    str(item.get("english_title") or item.get("japanese_title") or "")
                )
                if not gid or not title:
                    continue
                thumb = str(item.get("thumbnail") or "")
                out.append(
                    LookupResult(
                        title=title,
                        author="",
                        url=f"{self.BASE}/g/{gid}/",
                        cover_url=thumb,
                    )
                )
            return out

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return []

    async def cover(self, url: str, timeout: float) -> bytes | None:
        if not url:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_http_get, url, timeout, None, self.proxy), timeout
            )
        except Exception:
            return None


class EHentaiProvider:
    """e-hentai.org（表站，匿名可查）/ exhentai.org（里站，需要登录 cookie）。

    没有 API，只能解析 HTML。里站必须带 `ipb_member_id` + `ipb_pass_hash` 两个 cookie，
    否则返回登录跳转或 404。
    """

    prefix = "eh"

    def __init__(
        self, site: str = "e", member: str = "", pass_hash: str = "", proxy: str = ""
    ) -> None:
        self.site = "exhentai.org" if str(site).lower() == "ex" else "e-hentai.org"
        self.member = (member or "").strip()
        self.pass_hash = (pass_hash or "").strip()
        self.proxy = (proxy or "").strip()

    @property
    def base(self) -> str:
        return f"https://{self.site}"

    def _headers(self) -> dict[str, str]:
        if not (self.member and self.pass_hash):
            return {}
        return {
            "Cookie": f"ipb_member_id={self.member}; ipb_pass_hash={self.pass_hash}"
        }

    async def detail(self, gid_token: str, timeout: float) -> LookupResult | None:
        """gid_token 形如 `1234567/abcdef1234`（从 /g/<gid>/<token>/ 取）。"""
        import re as _re

        gid_token = str(gid_token or "").strip().strip("/")
        if not gid_token:
            return None

        def _f() -> LookupResult | None:
            url = f"{self.base}/g/{gid_token}/"
            raw = _http_get(url, timeout, self._headers(), self.proxy).decode("utf-8", "replace")
            # 标题：<title>xxx - E-Hentai Galleries</title>，或页面里的 id="gn"
            title = ""
            m = _re.search(r'id="gn"[^>]*>([^<]+)<', raw)
            if m:
                title = _norm_title(m.group(1))
            if not title:
                m = _re.search(r"<title>([^<]+)</title>", raw, _re.IGNORECASE)
                if m:
                    title = _norm_title(_re.sub(r"\s*-\s*E-Hentai Galleries\s*$", "", m.group(1)))
            if not title or "404" in title:
                return None
            # 作者：tag 区 <td class="tc">artist:</td><td>...<div|a>名字</...>
            author = ""
            m = _re.search(
                r'<td class="tc">artist:</td>\s*<td>(.*?)</td>', raw, _re.IGNORECASE | _re.S
            )
            if m:
                cell = m.group(1)
                m2 = _re.search(r'id="td_artist:[^"]*"[^>]*>([^<]+)<', cell)
                if not m2:
                    m2 = _re.search(r'>([^<>]{1,80})<', cell)
                if m2:
                    author = _norm_title(m2.group(1))
            # 封面：id="gd1" 里的 CSS background url(...)，不是 <img>
            cover = ""
            m = _re.search(r'id="gd1"[^>]*>.*?url\(([^)]+)\)', raw, _re.S)
            if m:
                cover = m.group(1).strip().strip("\"'")
            return LookupResult(
                title=title,
                author=author,
                url=url,
                cover_url=cover,
            )

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return None

    async def search(self, query: str, limit: int, timeout: float) -> list[LookupResult]:
        """`f_search` 走站点搜索，支持 `tag:xxx` 语法。"""
        import re as _re
        from urllib.parse import quote

        def _f() -> list[LookupResult]:
            url = f"{self.base}/?f_search={quote(query)}"
            raw = _http_get(url, timeout, self._headers(), self.proxy).decode("utf-8", "replace")
            # 搜索页里 gallery 链接与标题是分开的元素：
            #   <a href="https://e-hentai.org/g/<gid>/<token>/" ...>
            #   ...
            #   <a ... class="glink">标题</a>
            # 所以分别取两份列表，按出现顺序配对。
            links = _re.findall(
                r'href="(?:https?://[^/"]+)?/g/(\d+)/([0-9a-f]+)/"', raw, _re.IGNORECASE
            )
            titles = _re.findall(r'class="glink">([^<]{1,200})<', raw)
            out: list[LookupResult] = []
            seen: set[str] = set()
            for (gid, token), title in zip(links, titles):
                title = _norm_title(title)
                if not title or gid in seen:
                    continue
                seen.add(gid)
                out.append(
                    LookupResult(
                        title=title,
                        author="",
                        url=f"{self.base}/g/{gid}/{token}/",
                    )
                )
                if len(out) >= max(1, min(10, limit)):
                    break
            return out

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return []

    async def cover(self, url: str, timeout: float) -> bytes | None:
        if not url:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_http_get, url, timeout, self._headers(), self.proxy), timeout
            )
        except Exception:
            return None


# ---------------------------------------------------------------- 拷贝漫画 / G站漫画
# 两个站都有大量分流镜像，域名会漂。列表放在这里，配置里留空就按顺序探活取第一个能用的。
COPYMANGA_DOMAINS = [
    "www.copy5000.com",
    "www.copy4000.com",
    "www.copy3000.com",
    "copy3000.com",
    "www.2026copy.com",
    "2026copy.com",
    "www.copy20.com",
    "copy20.com",
]

# 包子系马甲。www.webmota.com 保留了老的 /search 与 /api/bzmhq 接口，
# 其余（godamh / baozimh / bzmh / g-mh）只支持详情，搜索会 404 —— 所以放第一位。
GMANGA_DOMAINS = [
    "www.webmota.com",
    "godamh.com",
    "baozimh.org",
    "bzmh.org",
    "g-mh.org",
    "m.baozimh.one",
    "m.g-mh.org",
]


# 今日推荐用的随机关键词池。两个中文站都支持关键词搜索，没有排行接口，
# 所以随机挑一个词去搜，再从结果里随机取一条。
DAILY_KEYWORDS = [
    "恋爱", "冒险", "日常", "奇幻", "校园", "悬疑", "热血", "治愈",
    "搞笑", "科幻", "古风", "都市", "推理", "青春", "美食", "运动",
]


def _probe_first(alive: list[str], timeout: float, proxy: str = "") -> str:
    """按顺序试，返回第一个 HTTP 200 的域名。全挂了返回空。"""
    for host in alive:
        try:
            _http_get(f"https://{host}/", timeout, proxy=proxy)
            return host
        except Exception:
            continue
    return ""


class CopyMangaProvider:
    """拷贝漫画：中文站，详情页可解析；搜索接口需要账号 token。

    页面里是 `Authorization: "Token " + token`，token 从 cookie 读，未登录时为空，
    所以 /api/v3/search/comic 会返回 200 但 total=0。
    """

    prefix = "copy"

    def __init__(self, domain: str = "", token: str = "", proxy: str = "") -> None:
        self.domain = (domain or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.proxy = (proxy or "").strip()
        self._picked = ""

    async def _host(self, timeout: float) -> str:
        if self.domain:
            return self.domain
        if self._picked:
            return self._picked
        host = await asyncio.to_thread(_probe_first, COPYMANGA_DOMAINS, timeout, self.proxy)
        self._picked = host
        return host

    def _headers(self) -> dict[str, str]:
        h = {"Referer": f"https://{self.domain or COPYMANGA_DOMAINS[0]}/"}
        if self.token:
            h["Authorization"] = f"Token {self.token}"
        return h

    async def detail(self, slug: str, timeout: float) -> LookupResult | None:
        import re as _re

        slug = str(slug or "").strip().strip("/")
        if not slug:
            return None

        def _f() -> LookupResult | None:
            host = self.domain or COPYMANGA_DOMAINS[0]
            url = f"https://{host}/comic/{slug}"
            raw = _http_get(url, timeout, self._headers(), self.proxy).decode("utf-8", "replace")
            # 优先 <h6 title="漫画名">，它只有名字；<title> 里带一堆描述词
            title = ""
            m = _re.search(r'<h6[^>]+title="([^"]{1,120})"', raw)
            if m:
                title = _norm_title(m.group(1))
            if not title:
                m = _re.search(r"<title>([^<]+)</title>", raw, _re.IGNORECASE)
                if m:
                    title = _norm_title(m.group(1).split("-")[0])
            if not title:
                return None
            author = ""
            m = _re.search(r'/author/[^"]+"[^>]*>([^<]+)<', raw)
            if m:
                author = _norm_title(m.group(1))
            cover = ""
            m = _re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', raw)
            if not m:
                m = _re.search(r'<img[^>]+class="[^"]*lazy[^"]*"[^>]+data-src="([^"]+)"', raw)
            if m:
                cover = m.group(1)
            desc = ""
            m = _re.search(r'class="intro"[^>]*>([^<]{5,400})<', raw)
            if m:
                desc = _norm_title(m.group(1))
            return LookupResult(title=title, author=author, url=url, cover_url=cover, desc=desc)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return None

    async def search(self, query: str, limit: int, timeout: float) -> list[LookupResult]:
        """搜索。API 路径不是固定的 —— 要从 /search 页面的 `const countApi = "..."` 读。

        参数：offset / platform=2 / limit / q / q_type=0（全站）。
        """
        import json
        import re as _re
        from urllib.parse import quote

        def _f() -> list[LookupResult]:
            host = self.domain or COPYMANGA_DOMAINS[0]
            # 1) 拿当前站点的搜索接口路径
            api = "/api/kb/web/searchcl/comics"
            try:
                page = _http_get(
                    f"https://{host}/search", timeout, self._headers(), self.proxy
                ).decode("utf-8", "replace")
                m = _re.search(r'const countApi = "([^"]+)"', page)
                if m:
                    api = m.group(1)
            except Exception:
                pass
            # 2) 搜索
            url = (
                f"https://{host}{api}?offset=0&platform=2&limit={max(1, min(10, limit))}"
                f"&q={quote(query)}&q_type=0"
            )
            raw = _http_get(url, timeout, self._headers(), self.proxy)
            d = json.loads(raw)
            out: list[LookupResult] = []
            for item in ((d.get("results") or {}).get("list") or [])[:limit]:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("path_word") or item.get("slug") or "")
                title = _norm_title(str(item.get("name") or ""))
                if not slug or not title:
                    continue
                author = ""
                for a in item.get("author") or []:
                    if isinstance(a, dict) and a.get("name"):
                        author = str(a["name"]).strip()
                        break
                cover = str(item.get("cover") or "")
                out.append(
                    LookupResult(
                        title=title,
                        author=author,
                        url=f"https://{host}/comic/{slug}",
                        cover_url=cover,
                    )
                )
            return out

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return []

    async def cover(self, url: str, timeout: float) -> bytes | None:
        if not url:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_http_get, url, timeout, self._headers(), self.proxy), timeout
            )
        except Exception:
            return None


class GMangaProvider:
    """包子系漫画（G站 / 包子漫画，同一套后端的一堆马甲）。

    详情路径按站点分两种：
      - godamh / g-mh / baozimh / bzmh：`/manga/<slug>`（Astro 新前端）
      - www.webmota.com：`/comic/<slug>`（老前端，但保留了 /search 接口）

    搜索只在老前端上可用：`/search?q=...` 返回服务端渲染的结果卡片
    （`<a href="/comic/<slug>" title="标题">`）。新前端的搜索是死代码，
    `/search?q=` 和 `/api/bzmhq/amp_comic_list` 在它们上面都 404。
    """

    prefix = "gm"

    def __init__(self, domain: str = "", proxy: str = "") -> None:
        self.domain = (domain or "").strip().rstrip("/")
        self.proxy = (proxy or "").strip()
        self._picked = ""

    async def _host(self, timeout: float) -> str:
        if self.domain:
            return self.domain
        if self._picked:
            return self._picked
        host = await asyncio.to_thread(_probe_first, GMANGA_DOMAINS, timeout, self.proxy)
        self._picked = host
        return host

    async def detail(self, slug: str, timeout: float) -> LookupResult | None:
        import re as _re

        slug = str(slug or "").strip().strip("/")
        if not slug:
            return None

        def _f() -> LookupResult | None:
            host = self.domain or GMANGA_DOMAINS[0]
            # 老前端（webmota）用 /comic/，新前端用 /manga/；两种都试一下
            paths = ["/comic/", "/manga/"] if "webmota" in host else ["/manga/", "/comic/"]
            raw, url = "", ""
            for prefix in paths:
                try:
                    candidate = f"https://{host}{prefix}{slug}"
                    raw = _http_get(candidate, timeout, proxy=self.proxy).decode("utf-8", "replace")
                    if "<title>" in raw and "404" not in raw[:2000]:
                        url = candidate
                        break
                except Exception:
                    continue
            if not url:
                return None
            title = ""
            m = _re.search(r"<title>([^<]+)</title>", raw, _re.IGNORECASE)
            if m:
                title = _norm_title(m.group(1))
                # 去掉前缀 emoji（站点标题形如 "🍘武煉巔峰 - 包子漫畫"）
                title = _re.sub(r"^[^\w\u4e00-\u9fff]+", "", title)
                # 去掉站名后缀
                title = _re.sub(
                    r"\s*[-|–]\s*(包子漫畫|包子漫画|G站漫畫|G站漫画|G社漫畫|G社漫画).*$",
                    "",
                    title,
                ).strip()
            if not title:
                return None
            author = ""
            m = _re.search(r"作者[是为：:]\s*([^\s，。,.<]{1,24})", raw)
            if m:
                author = _norm_title(m.group(1))
            if not author:
                # 老前端：<a href="/author/xxx">作者名</a>
                m = _re.search(r'href="/author/[^"]+"[^>]*>([^<]{1,40})<', raw)
                if m:
                    author = _norm_title(m.group(1))
            cover = ""
            m = _re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', raw)
            if m:
                cover = m.group(1)
            desc = ""
            m = _re.search(r'<meta[^>]+name="description"[^>]+content="([^"]{10,500})"', raw)
            if m:
                desc = _norm_title(m.group(1))
            return LookupResult(title=title, author=author, url=url, cover_url=cover, desc=desc)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return None

    async def search(self, query: str, limit: int, timeout: float) -> list[LookupResult]:
        """走老前端的服务端搜索。新前端没有可用的搜索入口，会自动回退到 webmota。"""
        import re as _re
        from urllib.parse import quote

        def _f() -> list[LookupResult]:
            hosts = [self.domain] if self.domain else list(GMANGA_DOMAINS)
            for host in hosts:
                try:
                    raw = _http_get(
                        f"https://{host}/search?q={quote(query)}", timeout, proxy=self.proxy
                    ).decode("utf-8", "replace")
                except Exception:
                    continue
                out: list[LookupResult] = []
                seen: set[str] = set()
                for m in _re.finditer(
                    r'href="/comic/([A-Za-z0-9_-]+)"[^>]*title="([^"]{1,120})"', raw
                ):
                    slug, title = m.group(1), _norm_title(m.group(2))
                    if not title or slug in seen:
                        continue
                    seen.add(slug)
                    out.append(
                        LookupResult(
                            title=title,
                            author="",
                            url=f"https://{host}/comic/{slug}",
                            cover_url=f"https://static-tw.baozimh.com/cover/{slug}.jpg",
                        )
                    )
                    if len(out) >= max(1, min(10, limit)):
                        break
                if out:
                    return out
            return []

        try:
            return await asyncio.wait_for(asyncio.to_thread(_f), timeout)
        except Exception:
            return []

    async def cover(self, url: str, timeout: float) -> bytes | None:
        if not url:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_http_get, url, timeout, None, self.proxy), timeout
            )
        except Exception:
            return None


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

    __ui_label__ = "麦麦漫画"
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
    allow_private: bool = Field(
        default=True, description="允许私聊使用",
        json_schema_extra={"label": "允许私聊使用", "hint": "开：私聊也可以触发查询；关：只允许群聊，私聊不响应。", "order": 26})
    private_send_cover: Literal["inherit", "on", "off"] = Field(
        default="inherit", description="私聊是否发图",
        json_schema_extra={"label": "私聊发图", "hint": "inherit=跟随「发送封面」；on=私聊总是发封面；off=私聊不发封面（只发标题）。", "order": 27})
    private_masking_enabled: Literal["inherit", "on", "off"] = Field(
        default="inherit", description="私聊是否打码",
        json_schema_extra={"label": "私聊打码", "hint": "inherit=跟随「启用打码服务」；on=私聊封面必打码；off=私聊不打码（也不发封面）。", "order": 28})

    # ---------------------------------------------------------------- 多渠道
    nhentai_enabled: bool = Field(
        default=True, description="启用 nhentai 渠道",
        json_schema_extra={"label": "启用 nhentai", "hint": "允许 nh 前缀与 /nh 命令。nhentai 有公开 API，不需要登录。", "order": 30})
    eh_enabled: bool = Field(
        default=True, description="启用 E-Hentai 渠道",
        json_schema_extra={"label": "启用 E-Hentai", "hint": "允许 eh 前缀与 /eh 命令。表站匿名可查；里站需要下面的 cookie。", "order": 31})
    eh_site: Literal["e", "ex"] = Field(
        default="e", description="E-Hentai 站点",
        json_schema_extra={"label": "E-Hentai 站点", "hint": "e=表站 e-hentai.org（匿名可访问，推荐）；ex=里站 exhentai.org（必须填 cookie，机房 IP 容易被封）。", "order": 32})
    eh_cookie_member: str = Field(
        default="", description="里站 cookie ipb_member_id",
        json_schema_extra={"label": "里站 ipb_member_id", "hint": "里站登录后从浏览器 cookie 复制 ipb_member_id 的值。仅「站点=里站」时需要，属凭据，勿外传。", "order": 33})
    eh_cookie_pass: str = Field(
        default="", description="里站 cookie ipb_pass_hash",
        json_schema_extra={"label": "里站 ipb_pass_hash", "hint": "同上，复制 ipb_pass_hash 的值。", "order": 34})

    # ---------------------------------------------------------------- 全年龄渠道
    copymanga_enabled: bool = Field(
        default=True, description="启用拷贝漫画",
        json_schema_extra={"label": "启用拷贝漫画", "hint": "全年龄中文漫画站。留空域名会自动在 8 个分流里探活。", "order": 39})
    copymanga_domain: str = Field(
        default="", description="拷贝漫画分流域名",
        json_schema_extra={"label": "拷贝漫画分流", "hint": "留空=自动选第一个能用的。可填 www.copy5000.com / www.2026copy.com 等。", "order": 40})
    copymanga_token: str = Field(
        default="", description="拷贝漫画 API token",
        json_schema_extra={"label": "拷贝漫画 token", "hint": "可选，一般不用填。搜索走公开接口（/search 页面里的 countApi），不需要登录；留空即可。", "order": 41})
    gmanga_enabled: bool = Field(
        default=True, description="启用 G站漫画",
        json_schema_extra={"label": "启用 G站漫画", "hint": "godamh 及同源镜像。留空域名会自动探活。", "order": 42})
    gmanga_domain: str = Field(
        default="", description="G站漫画分流域名",
        json_schema_extra={"label": "G站漫画分流", "hint": "留空=自动选。可填 godamh.com / baozimh.org / g-mh.org 等。", "order": 43})

    # ---------------------------------------------------------------- 封面
    sfw_cover_mask: bool = Field(
        default=False, description="全年龄站封面也打码",
        json_schema_extra={"label": "全年龄站也打码", "hint": "默认关闭：拷贝漫画 / G站漫画本身是全年龄站，封面直接发。开启后这些站的封面也会送打码服务（分类模型可能把擦边封面判成 hentai 而整图打码）。", "order": 54})

    # ---------------------------------------------------------------- 今日推荐
    daily_enabled: bool = Field(
        default=False, description="启用今日推荐",
        json_schema_extra={"label": "今日推荐", "hint": "每天定时从随机关键词里挑一部漫画推送到指定会话。", "order": 47})
    daily_time: str = Field(
        default="09:00", description="推送时间",
        json_schema_extra={"label": "推送时间", "hint": "24 小时制 HH:MM，如 09:00 / 21:30。只在插件运行期间生效。", "order": 48})
    daily_targets: list[str] = Field(
        default_factory=list, description="推送目标",
        json_schema_extra={"label": "推送目标", "hint": "群号或会话ID，逗号分隔。留空则不推送。", "order": 49})
    daily_count: int = Field(
        default=1, description="每次推荐条数",
        json_schema_extra={"label": "推荐条数", "hint": "每天推几条，1-5。", "order": 51})
    daily_cover: bool = Field(
        default=True, description="推荐带封面",
        json_schema_extra={"label": "带封面", "hint": "关闭则只推文字介绍。", "order": 52})
    daily_forward: bool = Field(
        default=True, description="推荐用合并转发",
        json_schema_extra={"label": "合并转发", "hint": "开启后推荐以聊天记录形式发出（文字+封面合成一条）。私聊不支持，会自动改分开发。", "order": 53})

    # ---------------------------------------------------------------- 文本
    t2s_enabled: bool = Field(
        default=True, description="自动转换繁简",
        json_schema_extra={"label": "自动转简体", "hint": "拷贝漫画 / G站漫画 返回的是繁体，开启后标题和作者统一转成简体。需要 zhconv。", "order": 46})

    # ---------------------------------------------------------------- R18 渠道门禁
    adult_declared: bool = Field(
        default=False, description="已阅读并同意 R18 声明",
        json_schema_extra={"label": "我已满 18 岁并同意", "hint": "勾选表示你已成年，并自行承担在所在群启用 R18 渠道的全部责任。", "order": 44})
    adult_enabled: bool = Field(
        default=False, description="启用 NSFW 渠道",
        json_schema_extra={"label": "启用 NSFW 渠道", "hint": "需先勾选上面的声明。开启后 JM / nhentai / E-Hentai 三个渠道的设置项才会显示。", "order": 45})
    adult_private: Literal["inherit", "on", "off"] = Field(
        default="inherit", description="私聊是否允许 NSFW",
        json_schema_extra={"label": "私聊 NSFW", "hint": "inherit=跟随总开关；on=私聊也允许；off=私聊一律禁止 NSFW 渠道。群聊可按群单独覆盖。", "order": 45})

    # ---------------------------------------------------------------- NSFW 内容过滤
    sfw_only: bool = Field(
        default=False, description="只搜全年龄内容",
        json_schema_extra={"label": "NSFW 开关（开=只搜全年龄）", "hint": "开启后搜索类命令只返回全年龄内容（nhentai 加 tag:non-h，E-Hentai 加 other:non-h）。关闭则不过滤。", "order": 35})

    # ---------------------------------------------------------------- 搜索
    search_enabled: bool = Field(
        default=True, description="启用名字搜索",
        json_schema_extra={"label": "搜名字给链接", "hint": "允许「/本搜 关键词」按标题搜索，返回标题 + 链接。", "order": 36})
    tag_search_enabled: bool = Field(
        default=True, description="启用标签搜索",
        json_schema_extra={"label": "标签搜索", "hint": "允许「/本标签 标签」按 tag 搜索，返回标题 + 链接。", "order": 37})
    search_max_results: int = Field(
        default=5, description="搜索结果条数上限",
        json_schema_extra={"label": "结果条数", "hint": "搜索类命令最多返回几条（1-10）。", "order": 38})


class RootConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    jm: JmConfig = Field(default_factory=JmConfig)


class JmLookupPlugin(MaiBotPlugin):
    config_model = RootConfig

    def __init__(self) -> None:
        super().__init__()
        self._states: dict[str, ConfigWidgetState] = {
            "api_status": ConfigWidgetState(text="未检测。", tone="neutral"),
            "proxy_status": ConfigWidgetState(text="未检测。", tone="neutral"),
            "domains_status": ConfigWidgetState(text="未检测。", tone="neutral"),
        }
        self._domain_rows: list[dict[str, Any]] = []
        self._copy_domain_rows: list[dict[str, Any]] = []
        self._gm_domain_rows: list[dict[str, Any]] = []
        self._callable_domains: list[str] = []
        self._session_overrides: dict[str, dict[str, Any]] = {}
        self._domain_task: asyncio.Task | None = None
        self._cache: dict[int, tuple[str, str] | None] = {}
        self._last: dict[str, float] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._daily_task: asyncio.Task | None = None
        self._daily_last: str = ""
        self._client: Any = None
        self._client_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期
    async def on_load(self) -> None:
        self._session_overrides = self._load_sessions()
        if self._daily_task is None or self._daily_task.done():
            self._daily_task = asyncio.create_task(self._daily_loop())
        self.ctx.logger.info("麦麦漫画插件已加载")

    async def on_unload(self) -> None:
        if self._daily_task and not self._daily_task.done():
            self._daily_task.cancel()
            self._daily_task = None
        self._cache.clear()
        self.ctx.logger.info("麦麦漫画插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("禁漫号查询插件配置已更新: version=%s", version)

    # ---------------------------------------------------------------- 会话级设置
    def _message_key(self, message: dict[str, Any]) -> str:
        gid = self._group_of(message)
        if gid:
            return str(gid)
        info = message.get("message_info") or {}
        ui = (info.get("user_info") if isinstance(info, dict) else None) or {}
        if isinstance(ui, dict) and ui.get("user_id"):
            return str(ui.get("user_id"))
        return str(message.get("session_id") or "")

    def _sessions_path(self):
        from pathlib import Path  # noqa: PLC0415

        try:
            base = Path(str(self.ctx.paths.data_dir))
        except Exception:
            base = Path("/tmp")
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = Path("/tmp")
        return base / "session_overrides.json"

    def _load_sessions(self) -> dict[str, dict[str, Any]]:
        import json  # noqa: PLC0415

        try:
            data = json.loads(self._sessions_path().read_text(encoding="utf-8"))
            sessions = data.get("sessions")
            return sessions if isinstance(sessions, dict) else {}
        except Exception:
            return {}

    def _save_sessions_file(self) -> None:
        import json  # noqa: PLC0415

        self._sessions_path().write_text(json.dumps({"sessions": self._session_overrides}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _eff_bool(self, key: str, session_key: str, default: bool) -> bool:
        val = (self._session_overrides.get(str(session_key)) or {}).get(key)
        return default if val is None else bool(val)

    @staticmethod
    def _tri_state(value: Any, default: bool) -> bool:
        if value == "on":
            return True
        if value == "off":
            return False
        return default

    def _effective_send_cover(self, cfg: JmConfig, session_key: str, is_private: bool) -> bool:
        val = bool(cfg.send_cover)
        if is_private:
            val = self._tri_state(getattr(cfg, "private_send_cover", "inherit"), val)
        return self._eff_bool("send_cover", session_key, val)

    def _effective_masking(self, cfg: JmConfig, session_key: str, is_private: bool) -> bool:
        val = bool(cfg.masking_enabled)
        if is_private:
            val = self._tri_state(getattr(cfg, "private_masking_enabled", "inherit"), val)
        return self._eff_bool("masking_enabled", session_key, val)

    def _write_toml_sections(self, sections: dict[str, dict[str, Any]]) -> None:
        """按 [section] 精确写回 config.toml（避免同名 key 串段）。"""
        import re as _re  # noqa: PLC0415
        import tomllib  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        path = Path(__file__).resolve().with_name("config.toml")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

        def fmt(v: Any) -> str:
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, (list, tuple)):
                return "[" + ", ".join(fmt(x) for x in v) + "]"
            return '"' + str(v).replace('"', '\\"') + '"'

        current = ""
        remaining = {sec: dict(vals) for sec, vals in sections.items()}
        out: list[str] = []
        for line in lines:
            m = _re.match(r"^\s*\[([^\]]+)\]\s*$", line)
            if m:
                current = m.group(1).strip()
                out.append(line)
                continue
            km = _re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*)=", line)
            if km and current in remaining:
                key = km.group(2)
                if key in remaining[current]:
                    out.append(f"{key} = {fmt(remaining[current].pop(key))}")
                    continue
            out.append(line)
        for sec, vals in remaining.items():
            if not vals:
                continue
            if not any(_re.match(r"^\s*\[" + _re.escape(sec) + r"\]\s*$", x) for x in out):
                out.append(f"[{sec}]")
            for k, v in vals.items():
                out.append(f"{k} = {fmt(v)}")
        text = "\n".join(out) + "\n"
        path.write_text(text, encoding="utf-8")
        try:
            self.set_plugin_config(tomllib.loads(text))
        except Exception:
            pass

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
        extra = dict(sync_to_maisaka_history=bool(cfg.inject_context), maisaka_source_kind="manga")
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
            await self.ctx.send.image(b64, stream_id, sync_to_maisaka_history=bool(cfg.inject_context), maisaka_source_kind="manga")
        except Exception as exc:
            self.ctx.logger.warning(f"[jm] 发送封面失败: {type(exc).__name__}: {exc}")

    async def _send_forward(
        self, stream_id: str, text: str, cover_b64: str, nick: str = "本子信息"
    ) -> None:
        cfg = self.config.jm
        nodes = [
            {"user_id": "", "nickname": nick, "content": [{"type": "text", "content": text}]},
            {"user_id": "", "nickname": "封面", "content": [{"type": "image", "content": cover_b64}]},
        ]
        try:
            if await self.ctx.send.forward(nodes, stream_id, sync_to_maisaka_history=bool(cfg.inject_context), maisaka_source_kind="manga"):
                return
        except Exception as exc:
            text_msg = str(exc)
            if "forward" in text_msg.lower() or "不支持" in text_msg:
                self.ctx.logger.info("[jm] 该会话不支持合并转发，改分开发送")
            else:
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
        if cfg.bundle_forward and cover_b64 and not reply_ctx.get("is_private"):
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

    def _cover_path_tag(self, tag: str) -> str:
        """按任意标签生成封面临时路径。跨渠道用不同 tag，避免 JM 1 与 nhentai 1 撞名。"""
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
        safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(tag))[:64] or "x"
        return os.path.join(d, f"cover_{safe}.jpg")

    def _is_private(self, kwargs: dict[str, Any]) -> bool:
        """是不是私聊。OneBot v11 标准模式没有 send_private_forward_msg，
        私聊里做合并转发必然失败，所以调用方要据此改用分开发。"""
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            return not self._group_of(msg)
        return False

    def _target_is_private(self, target: str) -> bool:
        """判断推送目标是不是私聊。

        MaiBot 的 stream_id 形如 `qq_group_771560629` / `qq_private_xxx`，
        也见过纯群号。所以先看关键字，再回退到纯数字判断。
        """
        t = str(target or "").strip()
        low = t.lower()
        if "group" in low or "群" in low:
            return False
        if "private" in low or "私聊" in low or "friend" in low:
            return True
        return not t.isdigit()

    def _session_ctx(self, kwargs: dict[str, Any]) -> tuple[str, bool]:
        """从命令参数里取 (session_key, is_private)。"""
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            sid = str(msg.get("session_id") or kwargs.get("stream_id") or "").strip()
            return sid, not self._group_of(msg)
        return str(kwargs.get("stream_id") or "").strip(), False

    def _adult_on(self, session_key: str = "", is_private: bool = False) -> bool:
        """NSFW 渠道是否已解锁。

        三层：总开关（声明+启用）→ 私聊三态 → 按群覆盖。
        """
        cfg = self.config.jm
        if not (
            getattr(cfg, "adult_declared", False) and getattr(cfg, "adult_enabled", False)
        ):
            return False
        if is_private:
            return self._tri_state(getattr(cfg, "adult_private", "inherit"), True)
        if session_key:
            return self._eff_bool("adult", session_key, True)
        return True

    def _proxy(self) -> str:
        """当前生效的代理地址（未启用则为空）。所有渠道共用这一个。"""
        cfg = self.config.jm
        if not getattr(cfg, "proxy_enabled", False):
            return ""
        return str(getattr(cfg, "proxy_url", "") or "").strip()

    def _t2s(self, text: str) -> str:
        """繁体转简体。拷贝漫画和包子系都是繁体站，统一转成简体更符合习惯。"""
        if not text or not getattr(self.config.jm, "t2s_enabled", True):
            return text
        try:
            import zhconv  # noqa: PLC0415

            return zhconv.convert(text, "zh-cn")
        except Exception:
            return text

    def _sfw_only(self) -> bool:
        return bool(getattr(self.config.jm, "sfw_only", False))

    def _search_query(self, q: str, site: str = "nh") -> str:
        """按 SFW 开关给搜索词加全年龄过滤。

        只有 nhentai（tag:non-h）和 E-Hentai（other:non-h）认这套语法；
        拷贝漫画 / G站本身就是全年龄站，加过滤词反而搜不到，直接返回原词。
        """
        q = (q or "").strip()
        if not self._sfw_only():
            return q
        if site == "nh":
            return f"{q} tag:non-h".strip()
        if site == "eh":
            return f"{q} other:non-h".strip()
        return q

    async def _prepare_cover_multi(self, provider: Any, result: LookupResult, tag: str) -> str | None:
        """按 NSFW 模式处理封面：text=不发 / masked=打码 / raw=原图。"""
        cfg = self.config.jm
        if not result.cover_url:
            return None
        data = await provider.cover(result.cover_url, float(cfg.request_timeout))
        if not data:
            return None
        # 全年龄站（拷贝漫画 / G站漫画）默认直接发原封面，不送打码
        if getattr(provider, "prefix", "") in ("copy", "gm") and not getattr(
            cfg, "sfw_cover_mask", False
        ):
            import base64 as _b64  # noqa: PLC0415

            return _b64.b64encode(data).decode()
        if not cfg.masking_enabled:
            return None
        path = self._cover_path_tag(tag)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception:
            return None
        b64 = await self._ahm_process(path)
        if b64 == "SKIP":
            return None
        return b64

    async def _handle_result(
        self, provider: Any, result: LookupResult, stream_id: str,
        reply_ctx: dict[str, Any], tag: str, extra_line: str = "",
        is_private: bool = False,
    ) -> None:
        """统一的多渠道发送：标题(+作者)(+链接) 与封面按 NSFW 模式处理。"""
        cfg = self.config.jm
        text = self._build_text((self._t2s(result.title), self._t2s(result.author)), cfg)
        if extra_line:
            text = f"{text}\n{extra_line}"
        cover_b64 = await self._prepare_cover_multi(provider, result, tag)
        # 私聊没有 send_private_forward_msg，只能分开发
        if cfg.bundle_forward and cover_b64 and not is_private:
            await self._send_forward(stream_id, text, cover_b64)
        else:
            await self._send(stream_id, text, reply_ctx)
            if cover_b64:
                await self._send_image(stream_id, cover_b64)

    def _providers(self) -> dict[str, Any]:
        """按配置组装可用渠道。"""
        cfg = self.config.jm
        out: dict[str, Any] = {"jm": None}
        if getattr(cfg, "nhentai_enabled", True):
            out["nh"] = NhentaiProvider(self._proxy())
        if getattr(cfg, "eh_enabled", True):
            out["eh"] = EHentaiProvider(
                str(getattr(cfg, "eh_site", "e") or "e"),
                str(getattr(cfg, "eh_cookie_member", "") or ""),
                str(getattr(cfg, "eh_cookie_pass", "") or ""),
                self._proxy(),
            )
        return out

    # ---------------------------------------------------------------- 今日推荐
    async def _daily_loop(self) -> None:
        """每分钟检查一次；到点且今天没推过就推一次。"""
        import datetime as _dt  # noqa: PLC0415

        await asyncio.sleep(20)  # 启动后缓一下，避开加载高峰
        while True:
            try:
                cfg = self.config.jm
                if getattr(cfg, "daily_enabled", False):
                    now = _dt.datetime.now()
                    hhmm = now.strftime("%H:%M")
                    today = now.strftime("%Y-%m-%d")
                    want = str(getattr(cfg, "daily_time", "09:00") or "09:00").strip()
                    if hhmm == want and self._daily_last != today:
                        self._daily_last = today
                        await self._daily_push()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.warning(f"[daily] 定时任务异常: {type(exc).__name__}: {exc}")
            await asyncio.sleep(45)

    def _channel_name(self, prov: Any) -> str:
        return {
            "copy": "拷贝漫画",
            "gm": "G站漫画",
            "nh": "nhentai",
            "eh": "E-Hentai",
            "jm": "禁漫",
        }.get(getattr(prov, "prefix", ""), "未知")

    def _daily_providers(self, session_key: str = "", is_private: bool = False) -> list[Any]:
        """所有已启用的渠道，每个渠道各出一本候选。NSFW 渠道按会话门禁过滤。"""
        cfg = self.config.jm
        out: list[Any] = []
        if getattr(cfg, "copymanga_enabled", True):
            out.append(
                CopyMangaProvider(
                    str(getattr(cfg, "copymanga_domain", "") or ""),
                    str(getattr(cfg, "copymanga_token", "") or ""),
                    self._proxy(),
                )
            )
        if getattr(cfg, "gmanga_enabled", True):
            out.append(
                GMangaProvider(str(getattr(cfg, "gmanga_domain", "") or ""), self._proxy())
            )
        if self._adult_on(session_key, is_private):
            if getattr(cfg, "nhentai_enabled", True):
                out.append(NhentaiProvider(self._proxy()))
            if getattr(cfg, "eh_enabled", True):
                out.append(
                    EHentaiProvider(
                        str(getattr(cfg, "eh_site", "e") or "e"),
                        str(getattr(cfg, "eh_cookie_member", "") or ""),
                        str(getattr(cfg, "eh_cookie_pass", "") or ""),
                        self._proxy(),
                    )
                )
        return out

    async def _llm_pick(self, cands: list[tuple[Any, LookupResult]], kw: str) -> int:
        """让 LLM 从候选里挑一本最合适的。失败就退回第一个。"""
        if len(cands) <= 1:
            return 0
        lines = [
            "你在给一个二次元 QQ 群挑今天的漫画推荐。下面是各渠道随机抓到的候选，",
            "请挑一部最适合推荐的：优先标题有吸引力、题材大众、不要太冷门。",
            "只回答一个数字，不要解释。",
            "",
        ]
        for i, (prov, it) in enumerate(cands, 1):
            lines.append(
                f"{i}. 《{self._t2s(it.title)}》 作者：{self._t2s(it.author) or '未知'} "
                f"来源：{self._channel_name(prov)}"
            )
        prompt = "\n".join(lines)
        try:
            res = await self.ctx.call_capability(
                "llm.generate", prompt=prompt, max_tokens=32, temperature=0.3
            )
            text = str((res or {}).get("response") or "").strip()
            import re as _re  # noqa: PLC0415

            m = _re.search(r"\d+", text)
            if m:
                idx = int(m.group()) - 1
                if 0 <= idx < len(cands):
                    self.ctx.logger.info(f"[daily] LLM 选中第 {idx + 1} 本（{text[:20]}）")
                    return idx
            self.ctx.logger.info(f"[daily] LLM 回复无法解析，用第一本: {text[:40]!r}")
        except Exception as exc:
            self.ctx.logger.warning(f"[daily] LLM 审核失败，用第一本: {type(exc).__name__}: {exc}")
        return 0

    async def _daily_build_one(
        self, session_key: str = "", is_private: bool = False
    ) -> tuple[Any, LookupResult, str, int] | None:
        """抓候选 + LLM 挑一本，返回 (provider, item, 关键词, 候选数)。"""
        import random  # noqa: PLC0415

        provs = self._daily_providers(session_key, is_private)
        if not provs:
            self.ctx.logger.info("[daily] 没有已启用的渠道")
            return None
        timeout = float(self.config.jm.request_timeout)
        kw = random.choice(DAILY_KEYWORDS)
        cands: list[tuple[Any, LookupResult]] = []
        for prov in provs:
            try:
                items = await prov.search(kw, 5, timeout)
            except Exception:
                items = []
            if items:
                cands.append((prov, random.choice(items)))
        if not cands:
            self.ctx.logger.info(f"[daily] 关键词「{kw}」在所有渠道都没搜到")
            return None
        self.ctx.logger.info(f"[daily] 候选 {len(cands)} 本，交给 LLM 审核")
        idx = await self._llm_pick(cands, kw)
        prov, item = cands[idx]
        return prov, item, kw, len(cands)

    async def _daily_push(self) -> None:
        cfg = self.config.jm
        targets = [str(t).strip() for t in (cfg.daily_targets or []) if str(t).strip()]
        if not targets:
            self.ctx.logger.info("[daily] 未配置推送目标，跳过")
            return
        built = await self._daily_build_one()
        if built is None:
            return
        prov, item, kw, cand_n = built
        for target in targets:
            try:
                await self._send_daily_one(target, prov, item, kw, cand_n)
            except Exception as exc:
                self.ctx.logger.warning(f"[daily] 推送到 {target} 失败: {type(exc).__name__}: {exc}")

    async def _try_send_forward(
        self, target: str, text: str, cover_b64: str, nick: str = "今日推荐"
    ) -> bool:
        """尝试合并转发。返回是否成功（适配器不支持时返回 False，不抛错）。"""
        cfg = self.config.jm
        nodes = [
            {"user_id": "", "nickname": nick, "content": [{"type": "text", "content": text}]},
            {"user_id": "", "nickname": "封面", "content": [{"type": "image", "content": cover_b64}]},
        ]
        try:
            ok = await self.ctx.send.forward(
                nodes, target, sync_to_maisaka_history=bool(cfg.inject_context),
                maisaka_source_kind="manga",
            )
            return bool(ok)
        except Exception as exc:
            msg = str(exc)
            if "forward" in msg.lower() or "不支持" in msg:
                self.ctx.logger.info("[daily] 适配器不支持合并转发，改用图文混合")
            else:
                self.ctx.logger.warning(f"[daily] 合并转发失败: {type(exc).__name__}: {exc}")
            return False

    async def _try_send_hybrid(self, target: str, text: str, cover_b64: str) -> bool:
        """尝试图文混合单条。失败返回 False。"""
        cfg = self.config.jm
        segments = [
            {"type": "text", "content": text},
            {"type": "image", "content": cover_b64},
        ]
        try:
            ok = await self.ctx.send.hybrid(
                segments, target, sync_to_maisaka_history=bool(cfg.inject_context),
                maisaka_source_kind="manga",
            )
            return bool(ok)
        except Exception as exc:
            self.ctx.logger.info(f"[daily] 图文混合不可用，改用分开发: {type(exc).__name__}")
            return False

    async def _send_daily_one(
        self, target: str, prov: Any, item: LookupResult, kw: str, cand_n: int = 1,
        is_private: bool | None = None,
    ) -> None:
        cfg = self.config.jm
        # 搜索结果往往缺作者/简介，选中后再查一次详情补全
        try:
            slug = item.url.rstrip("/").split("/")[-1]
            full = await prov.detail(slug, float(cfg.request_timeout))
            if full and full.title:
                item = full
        except Exception:
            pass
        lines = ["📚 今日推荐", ""]
        lines.append(f"《{self._t2s(item.title)}》")
        if item.author:
            lines.append(f"作者：{self._t2s(item.author)}")
        if item.desc:
            desc = self._t2s(item.desc).replace("\n", " ").strip()
            lines.append(f"简介：{desc[:120]}{'…' if len(desc) > 120 else ''}")
        lines.append(f"🔗 {item.url}")
        lines.append("")
        lines.append(f"（{self._channel_name(prov)} · 关键词「{kw}」· 从 {cand_n} 本里选出）")
        text = "\n".join(lines)
        cover_b64 = None
        if getattr(cfg, "daily_cover", True):
            cover_b64 = await self._prepare_cover_multi(prov, item, f"daily_{abs(hash(item.url)) % 10**8}")
        priv = self._target_is_private(target) if is_private is None else is_private
        want_fwd = bool(getattr(cfg, "daily_forward", True))
        sent = False
        # 1) 合并转发（真·聊天记录卡片）。OneBot v11 标准模式不支持，会抛错
        if want_fwd and cover_b64 and not priv:
            sent = await self._try_send_forward(target, text, cover_b64, nick="今日推荐")
        # 2) 图文混合单条（一条消息里文字+封面）
        if not sent and cover_b64:
            sent = await self._try_send_hybrid(target, text, cover_b64)
        # 3) 兜底：文字 + 图片分开发
        if not sent:
            await self._send(target, text, {})
            if cover_b64:
                await self._send_image(target, cover_b64)

    async def _search_channels(
        self, q: str, limit: int, timeout: float, tag_mode: bool = False,
        session_key: str = "", is_private: bool = False,
    ) -> list[LookupResult]:
        """跨渠道搜索。中文站优先，每个渠道都分到名额，NSFW 渠道受门禁控制。"""
        cfg = self.config.jm
        chans: list[Any] = []
        if getattr(cfg, "copymanga_enabled", True):
            chans.append(
                CopyMangaProvider(
                    str(getattr(cfg, "copymanga_domain", "") or ""),
                    str(getattr(cfg, "copymanga_token", "") or ""),
                    self._proxy(),
                )
            )
        if getattr(cfg, "gmanga_enabled", True):
            chans.append(
                GMangaProvider(str(getattr(cfg, "gmanga_domain", "") or ""), self._proxy())
            )
        if self._adult_on():
            if getattr(cfg, "nhentai_enabled", True):
                chans.append(NhentaiProvider(self._proxy()))
            if getattr(cfg, "eh_enabled", True):
                chans.append(
                    EHentaiProvider(
                        str(getattr(cfg, "eh_site", "e") or "e"),
                        str(getattr(cfg, "eh_cookie_member", "") or ""),
                        str(getattr(cfg, "eh_cookie_pass", "") or ""),
                        self._proxy(),
                    )
                )
        if not chans:
            return []
        per = max(2, limit // len(chans))

        def _query_for(prov: Any) -> str:
            if tag_mode:
                return f"tag:{q}" if prov.prefix == "nh" else q
            if prov.prefix == "nh":
                return self._search_query(q, "nh")
            if prov.prefix == "eh":
                return self._search_query(q, "eh")
            return q

        async def _one(prov: Any) -> list[LookupResult]:
            try:
                return await prov.search(_query_for(prov), per, timeout)
            except Exception:
                return []

        # 并发搜所有渠道，再交错合并：第一轮每个渠道各出 1 条，第二轮各出第 2 条…
        # 这样渠道多的时候不会出现「前面的把名额吃光、后面的永远看不到」。
        groups = await asyncio.gather(*[_one(p) for p in chans])
        out: list[LookupResult] = []
        for i in range(per):
            for grp in groups:
                if i < len(grp):
                    out.append(grp[i])
        return out[:limit]

    def _render_search(self, items: list[LookupResult], title: str) -> str:
        if not items:
            return "没搜到结果"
        lines = [title]
        for i, it in enumerate(items, 1):
            full = self._t2s(it.title)
            t = full if len(full) <= 70 else full[:67] + "..."
            lines.append(f"{i}. {t}")
            if it.author:
                lines.append(f"   作者：{self._t2s(it.author)}")
            if it.url:
                lines.append(f"   {it.url}")
        return "\n".join(lines)

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
        path = await self._download_cover(aid, float(cfg.request_timeout))
        if not path:
            return None
        if not cfg.masking_enabled:
            return None  # 用户选择不使用打码：不发封面（只发标题）
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
        """逐个分流做真实调用测试：API 分流用 jmcomic 拉专辑详情，HTML 分流测可达。"""
        cfg = self.config.jm
        proxy_url = str(getattr(cfg, "proxy_url", "") or "").strip()
        via_proxy = proxy_url if getattr(cfg, "proxy_enabled", False) and proxy_url else ""
        try:
            import jmcomic  # noqa: PLC0415
            from jmcomic import JmModuleConfig  # noqa: PLC0415

            api_domains = list(getattr(JmModuleConfig, "DOMAIN_API_LIST", []) or [])
            updated = list(getattr(JmModuleConfig, "DOMAIN_API_UPDATED_LIST", None) or [])
            if updated:
                api_domains = list(dict.fromkeys(api_domains + updated))
            JmModuleConfig.DEFAULT_PROXIES = jmcomic.ProxyBuilder.build_by_str(via_proxy) if via_proxy else {}
        except Exception as exc:
            return ConfigWidgetState(text=f"jmcomic 分流检测失败：{type(exc).__name__}: {exc}", tone="error")

        probe_album = 1448594

        def _api_call(domain: str) -> str:
            option = jmcomic.JmOption.default()
            option.client["domain"] = [domain]
            option.client["retry_times"] = 1
            client = option.new_jm_client()
            album = client.get_album_detail(probe_album)
            name = str(getattr(album, "name", "") or "").strip()
            if not name:
                raise RuntimeError("empty album name")
            return name

        async def _one_api(domain: str) -> dict[str, Any]:
            t0 = time.monotonic()
            try:
                name = await asyncio.wait_for(asyncio.to_thread(_api_call, domain), timeout=8.0)
                return {"kind": "API", "domain": domain, "ok": True, "status": "可调用",
                        "ms": int((time.monotonic() - t0) * 1000), "detail": name[:24]}
            except Exception as exc:
                return {"kind": "API", "domain": domain, "ok": False, "status": "不可调用",
                        "ms": int((time.monotonic() - t0) * 1000), "detail": type(exc).__name__}

        results = await asyncio.gather(*[_one_api(d) for d in api_domains])
        self._domain_rows = list(results)
        self._callable_domains = [r["domain"] for r in results if r.get("ok")]
        ok_n = sum(1 for r in results if r.get("ok"))
        lines = [f"jmcomic 分流：{ok_n}/{len(results)} 可调用"]
        for r in results:
            if r.get("ok"):
                lines.append(f"  ✅ [{r['kind']}] {r['domain']}  {r['status']}  {r['ms']}ms  {r['detail']}")
            else:
                lines.append(f"  ❌ [{r['kind']}] {r['domain']}  {r['status']}  {r['detail']}")
        tone = "error" if ok_n == 0 else ("warning" if ok_n < len(results) else "success")
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
        gid = self._group_of(message)
        if not gid:
            return bool(getattr(cfg, "allow_private", True))
        groups = [str(g).strip() for g in (cfg.allowed_groups or []) if str(g).strip()]
        if not groups:
            return True
        sid = str(message.get("session_id") or "")
        return gid in groups or (bool(sid) and sid in groups)

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
        if not self._adult_on(*self._session_ctx(kwargs)):
            return False, "NSFW 渠道未解锁", 1
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

    def _group_ok(self, kwargs: dict[str, Any]) -> bool:
        """命令入口用的权限检查（原 _group_allowed 是 hook 用的，签名不同，别重名）。"""
        cfg = self.config.jm
        groups = [str(g).strip() for g in (cfg.allowed_groups or []) if str(g).strip()]
        if not groups:
            return True
        stream_id = str(kwargs.get("stream_id") or "").strip()
        msg = kwargs.get("message") or {}
        gid = self._group_of(msg) if isinstance(msg, dict) else ""
        return bool((gid and gid in groups) or (stream_id in groups))

    def _reply_ctx_of(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            return self._reply_ctx_from_message(msg)
        if kwargs.get("message_id"):
            return {"message_id": str(kwargs.get("message_id")), "content": ""}
        return {}

    @Command("nh", pattern=r"^/(?:nh|nhentai)\s+(?P<id>\d{1,10})$")
    async def handle_nh_command(self, **kwargs):
        cfg = self.config.jm
        if not self._adult_on(*self._session_ctx(kwargs)):
            return False, "NSFW 渠道未解锁", 1
        if not getattr(cfg, "nhentai_enabled", True):
            return False, "nhentai 渠道未启用", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        gid = str((kwargs.get("matched_groups") or {}).get("id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not gid or not stream_id:
            return False, "用法：/nh 123456", 1
        prov = NhentaiProvider(self._proxy())
        res = await prov.detail(gid, float(cfg.request_timeout))
        if res is None:
            if cfg.on_not_found == "reply":
                await self._send(stream_id, cfg.not_found_text or "没搜到这个本子", self._reply_ctx_of(kwargs))
            return False, "没搜到", 1
        await self._handle_result(prov, res, stream_id, self._reply_ctx_of(kwargs), f"nh_{gid}", is_private=self._is_private(kwargs))
        return True, res.title, 1

    @Command("eh", pattern=r"^/(?:eh|ehentai)\s+(?P<id>\d{2,10}(?:/[0-9a-fA-F]{6,16})?)$")
    async def handle_eh_command(self, **kwargs):
        cfg = self.config.jm
        if not self._adult_on(*self._session_ctx(kwargs)):
            return False, "NSFW 渠道未解锁", 1
        if not getattr(cfg, "eh_enabled", True):
            return False, "E-Hentai 渠道未启用", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        raw = str((kwargs.get("matched_groups") or {}).get("id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not raw or not stream_id:
            return False, "用法：/eh 1234567/abcdef1234", 1
        prov = EHentaiProvider(
            str(getattr(cfg, "eh_site", "e") or "e"),
            str(getattr(cfg, "eh_cookie_member", "") or ""),
            str(getattr(cfg, "eh_cookie_pass", "") or ""),
            self._proxy(),
        )
        res = await prov.detail(raw, float(cfg.request_timeout))
        if res is None:
            hint = "没搜到（里站请先填 cookie）" if prov.site.startswith("ex") else "没搜到这个本子"
            if cfg.on_not_found == "reply":
                await self._send(stream_id, hint, self._reply_ctx_of(kwargs))
            return False, hint, 1
        await self._handle_result(prov, res, stream_id, self._reply_ctx_of(kwargs), f"eh_{raw}".replace("/", "_"), is_private=self._is_private(kwargs))
        return True, res.title, 1

    @Command("bensearch", pattern=r"^/(?:搜漫画|本搜|bensearch)\s+(?P<q>.+)$")
    async def handle_search_command(self, **kwargs):
        cfg = self.config.jm
        if not getattr(cfg, "search_enabled", True):
            return False, "名字搜索未启用", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        q = str((kwargs.get("matched_groups") or {}).get("q") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not q or not stream_id:
            return False, "用法：/搜漫画 关键词", 1
        limit = max(1, min(10, int(getattr(cfg, "search_max_results", 5) or 5)))
        timeout = float(cfg.request_timeout)
        _sk, _pv = self._session_ctx(kwargs)
        items = await self._search_channels(q, limit, timeout, session_key=_sk, is_private=_pv)
        text = self._render_search(items, f"搜索「{q}」")
        await self._send(stream_id, text, self._reply_ctx_of(kwargs))
        return True, text, 1

    @Command("bentag", pattern=r"^/(?:搜标签|本标签|bentag)\s+(?P<q>.+)$")
    async def handle_tag_command(self, **kwargs):
        cfg = self.config.jm
        if not getattr(cfg, "tag_search_enabled", True):
            return False, "标签搜索未启用", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        q = str((kwargs.get("matched_groups") or {}).get("q") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not q or not stream_id:
            return False, "用法：/搜标签 yuri", 1
        limit = max(1, min(10, int(getattr(cfg, "search_max_results", 5) or 5)))
        timeout = float(cfg.request_timeout)
        _sk, _pv = self._session_ctx(kwargs)
        items = await self._search_channels(q, limit, timeout, tag_mode=True, session_key=_sk, is_private=_pv)
        text = self._render_search(items, f"标签「{q}」")
        await self._send(stream_id, text, self._reply_ctx_of(kwargs))
        return True, text, 1

    @Command("copy", pattern=r"^/(?:copy|拷贝)\s+(?P<slug>[A-Za-z0-9_-]{2,80})$")
    async def handle_copy_command(self, **kwargs):
        cfg = self.config.jm
        if not getattr(cfg, "copymanga_enabled", True):
            return False, "拷贝漫画渠道未启用", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        slug = str((kwargs.get("matched_groups") or {}).get("slug") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not slug or not stream_id:
            return False, "用法：/copy <漫画slug>", 1
        prov = CopyMangaProvider(
            str(getattr(cfg, "copymanga_domain", "") or ""),
            str(getattr(cfg, "copymanga_token", "") or ""),
            self._proxy(),
        )
        res = await prov.detail(slug, float(cfg.request_timeout))
        if res is None:
            if cfg.on_not_found == "reply":
                await self._send(stream_id, cfg.not_found_text or "没搜到", self._reply_ctx_of(kwargs))
            return False, "没搜到", 1
        await self._handle_result(prov, res, stream_id, self._reply_ctx_of(kwargs), f"copy_{slug}", is_private=self._is_private(kwargs))
        return True, res.title, 1

    @Command("gm", pattern=r"^/(?:gm|g站)\s+(?P<slug>[A-Za-z0-9_-]{2,80})$")
    async def handle_gm_command(self, **kwargs):
        cfg = self.config.jm
        if not getattr(cfg, "gmanga_enabled", True):
            return False, "G站漫画渠道未启用", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        slug = str((kwargs.get("matched_groups") or {}).get("slug") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not slug or not stream_id:
            return False, "用法：/gm <漫画slug>", 1
        prov = GMangaProvider(
            str(getattr(cfg, "gmanga_domain", "") or ""), self._proxy()
        )
        res = await prov.detail(slug, float(cfg.request_timeout))
        if res is None:
            if cfg.on_not_found == "reply":
                await self._send(stream_id, cfg.not_found_text or "没搜到", self._reply_ctx_of(kwargs))
            return False, "没搜到", 1
        await self._handle_result(prov, res, stream_id, self._reply_ctx_of(kwargs), f"gm_{slug}", is_private=self._is_private(kwargs))
        return True, res.title, 1

    @Command("daily", pattern=r"^/(?:今日推荐|每日推荐|推荐|daily)$")
    async def handle_daily_command(self, **kwargs):
        cfg = self.config.jm
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return False, "用法：/今日推荐", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1
        _sk, _pv = self._session_ctx(kwargs)
        built = await self._daily_build_one(_sk, _pv)
        if built is None:
            return False, "没抓到合适的推荐，稍后再试", 1
        prov, item, kw, cand_n = built
        await self._send_daily_one(
            stream_id, prov, item, kw, cand_n, is_private=self._is_private(kwargs)
        )
        return True, f"已推送：{item.title}", 1

    @Command("jmhelp", pattern=r"^/(?:搜漫帮助|本帮助|jmhelp|帮助)$")
    async def handle_help_command(self, **kwargs):
        cfg = self.config.jm
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return False, "用法：/搜漫帮助", 1
        if not self._group_ok(kwargs):
            return False, "本群未启用该功能", 1

        adult = self._adult_on(*self._session_ctx(kwargs))
        lines = ["📖 麦麦漫画 · 帮助", "", "🔍 搜索"]
        if getattr(cfg, "search_enabled", True):
            lines.append("/搜漫画 <关键词>    跨渠道搜标题（中文站优先）")
        if getattr(cfg, "tag_search_enabled", True):
            lines.append("/搜标签 <标签>      按标签搜")
        if not (getattr(cfg, "search_enabled", True) or getattr(cfg, "tag_search_enabled", True)):
            lines.append("（搜索功能已关闭）")

        lines += ["", "📕 详情"]
        if getattr(cfg, "copymanga_enabled", True):
            lines.append("/copy <slug>       拷贝漫画")
        if getattr(cfg, "gmanga_enabled", True):
            lines.append("/gm <slug>         G站漫画")
        if adult:
            if getattr(cfg, "nhentai_enabled", True):
                lines.append("/nh <数字>         nhentai")
            if getattr(cfg, "eh_enabled", True):
                lines.append("/eh <id/token>     E-Hentai")
            lines.append("/jm <数字>         禁漫天堂")

        lines += ["", "🎲 今日推荐", "/今日推荐            立刻推一条随机推荐"]
        lines += ["", "⚙️ 其他", "/jmcheck           环境自检", "/搜漫帮助           本帮助", ""]

        ch = []
        if getattr(cfg, "copymanga_enabled", True):
            ch.append("拷贝漫画")
        if getattr(cfg, "gmanga_enabled", True):
            ch.append("G站漫画")
        if adult:
            if getattr(cfg, "nhentai_enabled", True):
                ch.append("nhentai")
            if getattr(cfg, "eh_enabled", True):
                ch.append("E-Hentai")
        lines.append("当前：" + (" · ".join(ch) if ch else "无可用渠道"))
        if adult:
            lines.append("NSFW：已解锁" + ("（只搜全年龄）" if self._sfw_only() else ""))
        else:
            lines.append("NSFW：未解锁")

        text = "\n".join(lines)
        await self._send(stream_id, text, self._reply_ctx_of(kwargs))
        return True, text, 1

    @HookHandler("chat.receive.after_process", name="manga_lookup_detect", description="检测漫画号并查询标题",
                 mode=HookMode.OBSERVE, order=HookOrder.NORMAL, timeout_ms=800, error_policy=ErrorPolicy.SKIP)
    async def on_message(self, **kwargs):
        cfg = self.config.jm
        if not self.config.plugin.enabled or not cfg.enabled:
            return {}
        # 自动识别的前缀（JM/禁漫/本子）全是 R18 站，没解锁就整条链路关掉
        if not self._adult_on(*self._session_ctx(kwargs)):
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
        _ctx = self._reply_ctx_from_message(message)
        _ctx["is_private"] = not self._group_of(message)
        task = asyncio.create_task(self._handle(int(ids[0]), stream_id, _ctx))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {}


    # ---------------------------------------------------------------- WebUI 页面 API
    def _domain_options(self) -> list[str]:
        """只列 API 分流域名（插件实际使用）；如有检测结果，只保留可调用的。"""
        opts: list[str] = ["auto"]
        candidates: list[str] = []
        try:
            from jmcomic import JmModuleConfig  # noqa: PLC0415

            candidates = list(getattr(JmModuleConfig, "DOMAIN_API_LIST", []) or [])
            updated = list(getattr(JmModuleConfig, "DOMAIN_API_UPDATED_LIST", None) or [])
            if updated:
                candidates = list(dict.fromkeys(candidates + updated))
        except Exception:
            candidates = []
        callable_domains = getattr(self, "_callable_domains", [])
        if callable_domains:
            candidates = [d for d in candidates if d in callable_domains] or list(callable_domains)
        for d in candidates:
            d = str(d).strip()
            if d and d not in opts:
                opts.append(d)
        return opts

    def _write_config_values(self, values: dict[str, Any]) -> None:
        """把少量配置写回插件目录 config.toml，并同步内存配置实例。"""
        import re as _re  # noqa: PLC0415
        import tomllib  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        path = Path(__file__).resolve().with_name("config.toml")
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            text = ""
        for key, val in values.items():
            if isinstance(val, bool):
                raw = "true" if val else "false"
            elif isinstance(val, (int, float)):
                raw = str(val)
            else:
                raw = '"' + str(val).replace('"', '\"') + '"'
            pattern = rf"(?m)^{_re.escape(key)}\s*=.*$"
            line = f"{key} = {raw}"
            if _re.search(pattern, text):
                text = _re.sub(pattern, line, text, count=1)
            else:
                if text and not text.endswith("\n"):
                    text += "\n"
                text += line + "\n"
        path.write_text(text, encoding="utf-8")
        try:
            self.set_plugin_config(tomllib.loads(text))
        except Exception:
            pass

    @API("webui_status", description="WebUI：当前设置与检测状态", version="1", public=False)
    async def api_webui_status(self):
        cfg = self.config.jm
        return {
            "api_url": str(cfg.ahm_api_url or ""),
            "proxy_enabled": bool(getattr(cfg, "proxy_enabled", False)),
            "proxy_url": str(getattr(cfg, "proxy_url", "") or ""),
            "jm_domain": str(getattr(cfg, "jm_domain", "") or ""),
            "api_status": self._states["api_status"].model_dump(),
            "proxy_status": self._states["proxy_status"].model_dump(),
            "domains_status": self._states["domains_status"].model_dump(),
            "domain_rows": self._domain_rows,
            "domain_options": self._domain_options(),
            "domain_checking": bool(self._domain_task and not self._domain_task.done()),
            "sessions": self._session_overrides,
        }

    @API("webui_check_api", description="WebUI：检测打码 API", version="1", public=False)
    async def api_webui_check_api(self):
        st = await self._check_api()
        self._states["api_status"] = st
        return {"text": st.text, "tone": st.tone}

    @API("webui_check_proxy", description="WebUI：测试代理", version="1", public=False)
    async def api_webui_check_proxy(self):
        st = await self._check_proxy()
        self._states["proxy_status"] = st
        return {"text": st.text, "tone": st.tone}

    @API("webui_check_domains", description="WebUI：后台检测 jmcomic 分流", version="1", public=False)
    async def api_webui_check_domains(self):
        if self._domain_task and not self._domain_task.done():
            return {"text": "检测进行中…", "tone": "neutral", "pending": True}

        async def _bg():
            try:
                self._states["domains_status"] = await self._check_domains()
            except Exception as exc:
                self._states["domains_status"] = ConfigWidgetState(text=f"检测失败：{type(exc).__name__}: {exc}", tone="error")

        self._domain_task = asyncio.create_task(_bg())
        return {"text": "检测已开始（约 5–15 秒），结果会自动刷新…", "tone": "neutral", "pending": True}

    async def _probe_domain_list(self, domains: list[str], timeout: float = 8.0) -> list[dict[str, Any]]:
        """并发探测一组域名，返回每条的可用性与耗时。"""
        import time as _time

        async def _one(host: str) -> dict[str, Any]:
            t0 = _time.monotonic()
            ok, info = False, ""
            try:
                import httpx  # noqa: PLC0415

                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
                    r = await cli.get(f"https://{host}/")
                    ok = r.status_code == 200
                    info = f"HTTP {r.status_code}"
            except Exception as exc:
                info = f"{type(exc).__name__}"
            return {
                "domain": host,
                "ok": ok,
                "ms": round((_time.monotonic() - t0) * 1000),
                "info": info,
            }

        return list(await asyncio.gather(*[_one(h) for h in domains]))

    def _domain_state(self, rows: list[dict[str, Any]], label: str) -> ConfigWidgetState:
        ok = [r for r in rows if r["ok"]]
        if not ok:
            return ConfigWidgetState(text=f"{label}：全部不可用", tone="error")
        best = min(ok, key=lambda r: r["ms"])
        return ConfigWidgetState(
            text=f"{label}：{len(ok)}/{len(rows)} 可用，最快 {best['domain']}（{best['ms']}ms）",
            tone="success",
        )

    @API("webui_check_copy_domains", description="WebUI：检测拷贝漫画分流", version="1", public=False)
    async def api_webui_check_copy_domains(self):
        rows = await self._probe_domain_list(COPYMANGA_DOMAINS)
        self._copy_domain_rows = rows
        return {"rows": rows, "state": self._domain_state(rows, "拷贝漫画").model_dump()}

    @API("webui_check_gm_domains", description="WebUI：检测 G站漫画分流", version="1", public=False)
    async def api_webui_check_gm_domains(self):
        rows = await self._probe_domain_list(GMANGA_DOMAINS)
        self._gm_domain_rows = rows
        return {"rows": rows, "state": self._domain_state(rows, "G站漫画").model_dump()}

    @API("webui_save_copy_domain", description="WebUI：保存拷贝漫画分流", version="1", public=False)
    async def api_webui_save_copy_domain(self, copymanga_domain: str = ""):
        val = (copymanga_domain or "").strip()
        self._write_toml_sections({"jm": {"copymanga_domain": val}})
        return {"copymanga_domain": val}

    @API("webui_save_gm_domain", description="WebUI：保存 G站漫画分流", version="1", public=False)
    async def api_webui_save_gm_domain(self, gmanga_domain: str = ""):
        val = (gmanga_domain or "").strip()
        self._write_toml_sections({"jm": {"gmanga_domain": val}})
        return {"gmanga_domain": val}

    @API("webui_settings", description="WebUI：读取全部设置", version="1", public=False)
    async def api_webui_settings(self):
        data = self._plugin_config_data or {}
        tpl = self.config.plugin
        jm = self.config.jm.model_dump()
        return {
            "plugin": {"enabled": bool(tpl.enabled)},
            "jm": jm,
            "sessions": self._session_overrides,
        }

    @API("webui_save_settings", description="WebUI：保存全部设置", version="1", public=False)
    async def api_webui_save_settings(self, payload: str = ""):
        import json  # noqa: PLC0415

        try:
            data = json.loads(payload or "{}")
        except Exception as exc:
            raise ValueError(f"参数格式错误: {exc}")
        if not isinstance(data, dict):
            raise ValueError("payload 必须是对象")
        jm_in = data.get("jm") or {}
        plugin_in = data.get("plugin") or {}
        allowed = set(JmConfig.model_fields.keys())
        values = {k: v for k, v in jm_in.items() if k in allowed and k != "config_version"}
        if values:
            try:
                JmConfig(**{**self.config.jm.model_dump(), **values})
            except Exception as exc:
                raise ValueError(f"配置校验失败: {exc}")
        sections = {}
        if values:
            sections["jm"] = values
        if "enabled" in plugin_in:
            sections["plugin"] = {"enabled": bool(plugin_in["enabled"])}
        if sections:
            self._write_toml_sections(sections)
        return {"saved": True, "jm": values}

    @API("webui_save_session", description="WebUI：保存分群/私聊覆盖", version="1", public=False)
    async def api_webui_save_session(self, payload: str = ""):
        import json  # noqa: PLC0415

        try:
            data = json.loads(payload or "{}")
        except Exception as exc:
            raise ValueError(f"参数格式错误: {exc}")
        key = str(data.get("session") or "").strip()
        if not key:
            raise ValueError("请填写群号或私聊会话ID")
        entry: dict[str, Any] = {}
        for field in ("send_cover", "masking_enabled"):
            val = data.get(field)
            if val is None or val == "inherit":
                continue
            entry[field] = bool(val)
        if entry:
            self._session_overrides[key] = entry
        else:
            self._session_overrides.pop(key, None)
        self._save_sessions_file()
        return {"session": key, "entry": entry, "sessions": self._session_overrides}

    @API("webui_delete_session", description="WebUI：删除分群/私聊覆盖", version="1", public=False)
    async def api_webui_delete_session(self, payload: str = ""):
        import json  # noqa: PLC0415

        try:
            data = json.loads(payload or "{}")
        except Exception as exc:
            raise ValueError(f"参数格式错误: {exc}")
        key = str(data.get("session") or "").strip()
        self._session_overrides.pop(key, None)
        self._save_sessions_file()
        return {"session": key, "sessions": self._session_overrides}

    @API("webui_save_domain", description="WebUI：保存指定分流", version="1", public=False)
    async def api_webui_save_domain(self, jm_domain: str = ""):
        val = str(jm_domain or "").strip()
        if val and val != "auto" and val not in self._domain_options():
            raise ValueError(f"未知分流: {val}")
        saved = "" if val in ("", "auto") else val
        self._write_config_values({"jm_domain": saved})
        return {"jm_domain": saved}


def create_plugin() -> JmLookupPlugin:
    return JmLookupPlugin()
