"""原生 Chromium 上下文初始化、公共静态缓存与传输统计。"""
import asyncio
import json
import logging
import os
import re
import sys
from urllib.parse import urlsplit
from utils.http_cache import LocalHttpCache, transfer_body_size

log = logging.getLogger(__name__)

# Python 路由级 HTTP 磁盘强缓存目录（位于 data/python_http_cache）
PYTHON_HTTP_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "python_http_cache")
)
os.makedirs(PYTHON_HTTP_CACHE_DIR, exist_ok=True)


class TrafficStats:
    """代理连接字节或 HTTP 估算；缓存节省量使用压缩传输体积。"""
    def __init__(self, proxy_bridge=None):
        self.net_transfer_bytes = 0
        self.cache_saved_bytes = 0
        self.cache_hit_count = 0
        self.cache_replayed_bytes = 0
        self.cache_size_unknown_hits = 0
        self.transfer_size_unknown_responses = 0
        self.blocked_count = 0
        self.telemetry_blocked_count = 0
        self.proxy_bridge = proxy_bridge
        self.network_requests = []
        self.mfa_diag = []  # 仅诊断：记录 2FA initializemobileapp 请求的次数/大小/结构（不影响任何请求）

    def set_proxy_bridge(self, bridge):
        self.proxy_bridge = bridge

    def record_transfer(self, num_bytes: int, url: str = "", r_type: str = "", status: int = 200):
        if num_bytes > 0:
            self.net_transfer_bytes += num_bytes
            if url:
                self.network_requests.append((num_bytes, url, r_type, status))

    def record_cache_hit(self, body_size: int, wire_size: int | None = None):
        self.cache_hit_count += 1
        self.cache_replayed_bytes += body_size
        if wire_size is None:
            self.cache_size_unknown_hits += 1
        else:
            self.cache_saved_bytes += wire_size

    def record_blocked(self, est_size: int = 0):
        # 未下载的媒体没有实际传输尺寸，只记录次数。
        self.blocked_count += 1

    def record_response(self, response, request=None, decoded_size=None):
        request = request or response.request
        req_size = 250 + sum(len(k) + len(v) + 4 for k, v in request.headers.items())
        payload = request.post_data_buffer
        req_size += len(payload) if payload else 0
        headers = response.headers
        body_size = 0 if response.status in (204, 304) or request.method == "HEAD" else transfer_body_size(headers, decoded_size)
        if body_size is None:
            self.transfer_size_unknown_responses += 1
            body_size = 0
        header_size = 250 + sum(len(k) + len(v) + 4 for k, v in headers.items())
        self.record_transfer(req_size + header_size + body_size, response.url, request.resource_type, response.status)

    def transfer_source(self):
        return "代理连接累计传输" if self.proxy_bridge and hasattr(self.proxy_bridge, "get_total_bytes") and self.proxy_bridge.get_total_bytes() > 0 else "HTTP 传输估算"

    def get_transfer_bytes(self) -> int:
        if self.proxy_bridge and hasattr(self.proxy_bridge, "get_total_bytes"):
            bridge_bytes = self.proxy_bridge.get_total_bytes()
            if bridge_bytes > 0:
                return bridge_bytes
        return self.net_transfer_bytes

    def get_transfer_mb(self) -> float:
        return self.get_transfer_bytes() / (1024 * 1024)

    def get_cache_saved_mb(self) -> float:
        return self.cache_saved_bytes / (1024 * 1024)

    def get_top_downloads(self, limit=5) -> list[tuple[float, str, str]]:
        sorted_reqs = sorted(self.network_requests, key=lambda x: x[0], reverse=True)
        return [(req[0] / 1024, req[1], req[2]) for req in sorted_reqs[:limit]]

    def add_response(self, response):
        try:
            self.record_response(response)
        except Exception:
            pass

    def get_mb(self) -> float:
        return self.get_transfer_mb()


# 1x1 透明标准 PNG / SVG / 空字体 极简数据
async def setup_save_data_route(ctx, stats: TrafficStats = None):
    """公共静态资源持久缓存，业务 API 放行，媒体拦截与遥测本地响应。"""
    # 预热本地 ExtensionManifest 规范化类型索引
    LocalHttpCache.init_canonical_manifests()
    try:
        LocalHttpCache.cleanup()
    except OSError as error:
        log.warning("静态缓存清理暂未完成: %s", error)

    BLOCKED_MEDIA_EXTS = (
        ".mp4", ".webm", ".ogg", ".mp3", ".wav",
        ".zip", ".iso", ".exe", ".msi", ".rar", ".7z", ".tar", ".gz",
        ".dmg", ".pkg", ".bin", ".apk", ".m4s", ".ts", ".flv", ".m3u8"
    )

    fulfilled_request_ids = set()
    block_telemetry = os.getenv("BLOCK_TELEMETRY", "true").strip().lower() not in ("0", "false", "no", "off")

    async def _route_handler(route, request):
        r_type = request.resource_type
        url = request.url
        url_lower = url.lower()
        clean_url = url_lower.split("?")[0].split("#")[0]

        # 明确的遥测端点在 POST 放行规则之前处理。返回成功，避免客户端错误重试。
        parsed = urlsplit(url)
        telemetry_endpoint = (
            (parsed.hostname == "browser.events.data.microsoft.com" and parsed.path.rstrip("/").lower() == "/onecollector/1.0")
            or (parsed.hostname == "portal.azure.com" and parsed.path.rstrip("/").lower() == "/api/telemetry")
        )
        if (block_telemetry and telemetry_endpoint and request.method in ("GET", "POST", "OPTIONS")
                and r_type != "document" and not request.is_navigation_request()):
            headers = {"cache-control": "no-store"}
            origin = request.headers.get("origin")
            if origin:
                headers.update({"access-control-allow-origin": origin, "access-control-allow-credentials": "true", "vary": "Origin"})
            if request.method == "OPTIONS":
                headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
                headers["access-control-allow-headers"] = request.headers.get("access-control-request-headers", "content-type")
            fulfilled_request_ids.add(id(request))
            await route.fulfill(status=204, headers=headers, body=b"")
            if stats:
                stats.telemetry_blocked_count += 1
            return

        # 1. 核心业务导航、主 HTML 文档以及所有非 GET 请求 (POST/PUT/DELETE/OPTIONS)：100% 原生直连（保证 Cookie、Session、登录跳转与 API 完整性）
        if r_type == "document" or request.is_navigation_request() or request.method != "GET":
            await route.continue_()
            return

        # 2. 拦截超大体积无用媒体与安装包文件 (.mp4, .zip, .iso, .exe, .msi 等)
        if clean_url.endswith(BLOCKED_MEDIA_EXTS):
            if stats:
                stats.record_blocked(est_size=5000000)
            await route.abort()
            return

        # 动态认证/API 放行；认证域名的公共脚本、CSS、字体可进入静态缓存。
        parsed = urlsplit(url)
        host = parsed.hostname
        public_auth_asset = host in ("mysignins.microsoft.com", "login.microsoftonline.com") and LocalHttpCache.is_cacheable(url)
        core_request = host in ("mysignins.microsoft.com", "login.microsoftonline.com", "management.azure.com", "graph.microsoft.com") or host in ("portal.azure.com", "signup.azure.com") and parsed.path.lower().startswith("/api/")
        if core_request and not public_auth_asset:
            await route.continue_()
            return

        if LocalHttpCache.is_cacheable(url, "GET"):
            # 一个账号下载时，其余账号等待并复用结果；所有等待结束后释放锁索引。
            async with LocalHttpCache.download_lock(url):
                is_manifest = "extensionmanifest/" in url_lower
                cached = LocalHttpCache.get_canonical_manifest(url) if is_manifest else LocalHttpCache.get(url)
                if cached:
                    body, headers, status = cached
                    if stats:
                        stats.record_cache_hit(len(body), LocalHttpCache.get_wire_size(url))
                    fulfilled_request_ids.add(id(request))
                    await route.fulfill(body=body, headers=headers, status=status)
                    return

                candidate = LocalHttpCache.manifest_candidate(url) if is_manifest else None
                fetch_kwargs = {}
                if candidate:
                    etag = candidate[1].get("headers", {}).get("etag")
                    if etag:
                        fetch_kwargs["headers"] = {**request.headers, "If-None-Match": etag}
                try:
                    fetched = await route.fetch(**fetch_kwargs)
                except Exception:
                    # fetch 失败后允许浏览器正常请求，不计为缓存命中。
                    await route.continue_()
                    return

                if candidate and fetched.status == 304:
                    body, meta = candidate
                    headers = dict(meta["headers"])
                    if fetched.headers.get("etag"):
                        headers["etag"] = fetched.headers["etag"]
                    try:
                        LocalHttpCache.save_canonical_manifest(url, body, headers, meta.get("wire_body_bytes"))
                    except OSError:
                        pass
                    if stats:
                        stats.record_response(fetched, request)
                        stats.record_cache_hit(len(body), meta.get("wire_body_bytes"))
                    fulfilled_request_ids.add(id(request))
                    await route.fulfill(body=body, headers=headers, status=200)
                    return

                body = await fetched.body()
                if stats:
                    stats.record_response(fetched, request, len(body))
                if fetched.status == 200:
                    try:
                        LocalHttpCache.put(url, body, fetched.headers, fetched.status)
                        if is_manifest:
                            LocalHttpCache.save_canonical_manifest(url, body, fetched.headers)
                    except OSError as error:
                        log.warning("静态缓存写入失败，已继续返回网络响应: %s", error)
                fulfilled_request_ids.add(id(request))
                await route.fulfill(response=fetched, body=body)
                return

        # 7. 核心风控/验证码挑战接口、Microsoft 动态登录认证接口、2FA 注册密钥接口与 ARM 提 Key 接口：100% 绝对原生放行
        if any(p in url_lower for p in LocalHttpCache.DYNAMIC_SECURITY_PATTERNS):
            await route.continue_()
            return

        # 8. 拦截大体积音视频及安装包
        if r_type == "media" or clean_url.endswith(BLOCKED_MEDIA_EXTS):
            if stats:
                stats.record_blocked(est_size=100000)
            await route.abort()
            return

        # 9. 其他所有核心请求 100% 原生放行（绝不拦截、绝不阻断）
        await route.continue_()

    await ctx.route("**/*", _route_handler)

    def _on_response_measure(response):
        try:
            req = response.request
            req_id = id(req)
            if req_id in fulfilled_request_ids:
                fulfilled_request_ids.discard(req_id)
                return  # 本地强缓存与本地 Mock 命中，不计入网络传输！

            if stats:
                stats.record_response(response, req)
            resp_bytes = transfer_body_size(response.headers) or 0
            if req.resource_type == "document" and urlsplit(response.url).hostname == "portal.azure.com":
                async def learn_portal_hashes():
                    try:
                        LocalHttpCache.learn_hashes_from_html(await response.body())
                    except Exception:
                        pass
                asyncio.ensure_future(learn_portal_hashes())

            # 记录 2FA initializemobileapp 的调用与秒级捕获 TOTP 密钥
            if "initializemobileapp" in (response.url or "").lower():
                asyncio.ensure_future(_diag_mfa_response(response, req, resp_bytes))
        except Exception:
            pass

    async def _diag_mfa_response(response, req, resp_bytes):
        entry = {"size": resp_bytes, "status": response.status, "enc": "", "post": "", "fields": ""}
        try:
            entry["enc"] = response.headers.get("content-encoding", "none")
            pd = req.post_data or ""
            entry["post"] = pd[:200]
            body = await response.body()  # 读取浏览器内存中的已下载数据，不产生额外流量
            entry["raw_len"] = len(body)
            try:
                data = json.loads(body.decode("utf-8", errors="ignore"))
                if isinstance(data, dict):
                    # 关键突破：直接从 API 响应中提取 16 位 TOTP SecretKey 并挂载到上下文
                    sec = data.get("SecretKey")
                    if sec and isinstance(sec, str):
                        clean_sec = re.sub(r"[\s-]+", "", sec.upper())
                        if len(clean_sec) >= 16:
                            ctx._captured_totp_secret = clean_sec
                            for p in getattr(ctx, "pages", []):
                                try:
                                    p._captured_totp_secret = clean_sec
                                except Exception:
                                    pass
                            cb_func = getattr(ctx, "_on_totp_captured", None)
                            if callable(cb_func):
                                try:
                                    cb_func(clean_sec)
                                except Exception:
                                    pass
                            log.info(f"[2FA] 网络层秒级捕获 SecretKey: {clean_sec}")
                    parts = []
                    for k, v in data.items():
                        ln = len(v) if isinstance(v, (str, list, dict)) else len(str(v))
                        parts.append((ln, f"{k}({type(v).__name__}:{ln})"))
                    parts.sort(reverse=True)
                    entry["fields"] = ", ".join(p[1] for p in parts[:8])
            except Exception:
                entry["fields"] = "non-json"
        except Exception as e:
            entry["fields"] = f"diag-error: {e}"[:100]
        if stats is not None:
            stats.mfa_diag.append(entry)

    ctx.on("response", _on_response_measure)


async def new_fingerprint_context(pw, headless: bool, proxy_config: dict | None,
                                   exe_path: str | None, slow_mo: int = 80,
                                   enable_save_data: bool = True):
    """创建使用原生浏览器参数的独立 Playwright BrowserContext。
    返回 (browser, ctx, fp, stats)。
    """
    if sys.platform != "win32" and "DISPLAY" not in os.environ:
        if not headless:
            log.info("[提示] 检测到 Linux 纯终端环境 (无 XServer/DISPLAY)，全自动强制开启无头模式 (headless=True)")
            headless = True

    # 保留 Chromium 自身的 UA、Client Hints、平台与硬件 API。
    browser_args = [
        "--lang=en-US",
        "--accept-lang=en-US",
        "--disable-dev-shm-usage",
        "--enforce-webrtc-ip-permission-check",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-pings",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-sync",
        "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider",
    ]
    if sys.platform != "win32":
        browser_args.append("--no-sandbox")

    if not exe_path or not os.path.exists(exe_path):
        import config
        exe_path = config.ensure_chromium_installed()

    launch_kwargs = {
        "headless": headless,
        "slow_mo": slow_mo,
        "proxy": proxy_config,
        "args": browser_args,
    }
    if exe_path and os.path.exists(exe_path):
        launch_kwargs["executable_path"] = exe_path

    try:
        browser = await pw.chromium.launch(**launch_kwargs)
    except Exception as e:
        log.warning(f"首次启动 Playwright Chromium 路径失败 ({e})，正在清理路径参数进行智能兼容启动...")
        clean_kwargs = {k: v for k, v in launch_kwargs.items() if k != "executable_path"}
        try:
            browser = await pw.chromium.launch(**clean_kwargs)
        except Exception as e2:
            if sys.platform == "win32":
                try:
                    browser = await pw.chromium.launch(**clean_kwargs, channel="msedge")
                except Exception:
                    browser = await pw.chromium.launch(**clean_kwargs, channel="chrome")
            else:
                raise e2

    # 英文业务界面保证现有按钮定位稳定；不再随代理出口随机切换语言。
    context_kwargs = {
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
    }
    try:
        ctx = await browser.new_context(**context_kwargs)
        ctx._captured_totp_secret = ""
        probe = await ctx.new_page()
        try:
            native = await probe.evaluate("""() => ({
                user_agent: navigator.userAgent,
                platform: navigator.platform,
                locale: navigator.language,
                languages: Array.from(navigator.languages),
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                viewport: {width: innerWidth, height: innerHeight}
            })""")
        finally:
            await probe.close()
        fp = {**native, "chrome_ver": browser.version,
              "major_ver": browser.version.split(".")[0], "mode": "native"}
    except BaseException:
        await browser.close()
        raise

    stats = TrafficStats()
    if enable_save_data:
        await setup_save_data_route(ctx, stats)
    else:
        ctx.on("response", stats.record_response)

    async def _fallback_mfa_capture(response):
        if "initializemobileapp" in (response.url or "").lower():
            try:
                body = await response.body()
                data = json.loads(body.decode("utf-8", errors="ignore"))
                if isinstance(data, dict):
                    sec = data.get("SecretKey")
                    if sec and isinstance(sec, str):
                        clean_sec = re.sub(r"[\s-]+", "", sec.upper())
                        if len(clean_sec) >= 16:
                            ctx._captured_totp_secret = clean_sec
                            for p in getattr(ctx, "pages", []):
                                try:
                                    p._captured_totp_secret = clean_sec
                                except Exception:
                                    pass
                            cb_func = getattr(ctx, "_on_totp_captured", None)
                            if callable(cb_func):
                                try:
                                    cb_func(clean_sec)
                                except Exception:
                                    pass
            except Exception:
                pass

    ctx.on("response", _fallback_mfa_capture)

    return browser, ctx, fp, stats
