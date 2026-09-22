"""
Azure Key 提取主流水线
-----------------------
每个账号完整流程:
  1. 启动带代理+指纹的浏览器
  2. 打开 Azure for Students 注册链接
  3. 登录（选「Use a different account」→ 输入 edu 邮箱+密码）
  4. 处理 MFA 注册（提取 TOTP secret）
  5. 填写 Your profile 表单（edu 邮箱直接填入，无需验证码）
  6. 等待账户确认 → 进入 portal
  7. 新开标签页访问 Education Software 页面
  8. 处理 Terms Acceptance
  9. 逐一搜索并提取 6 个产品的 key
  10. 保存结果到 JSONL + Excel
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from modules.azure_login import do_azure_login, LoginNetworkError, EmailInputTimeoutError, AzureCaptchaError
from modules.azure_signup import fill_azure_profile_form, SheerIDVerificationError
from modules.key_extractor import extract_all_keys
from utils.fingerprint import new_fingerprint_context
from utils.xray_proxy import XrayProxyChain
from utils.captcha_detector import check_captcha_present
from utils.process_manager import BrowserProcessManager, get_playwright_driver_pid, safe_close_playwright

log = logging.getLogger(__name__)
ProgressCallback = Optional[Callable[[str], None]]

_RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


class ProxyRotator:
    """并发安全的代理池轮换调度器。"""
    def __init__(self, proxy_pool: list[str]):
        self.proxy_pool = proxy_pool
        self._idx = 0
        self._lock = asyncio.Lock()

    async def get_next_proxy(self) -> tuple[str | None, int]:
        """获取下一个可用代理节点及其索引。"""
        if not self.proxy_pool:
            return None, 0
        async with self._lock:
            curr_idx = self._idx % len(self.proxy_pool)
            p = self.proxy_pool[curr_idx]
            self._idx += 1
            return p, curr_idx


# ── 数据类 ────────────────────────────────────────────────────
@dataclass
class Account:
    email:       str   # edu 邮箱/微软账号邮箱
    password:    str
    totp_secret: str = ""



@dataclass
class KeyResult:
    account:     Account
    success:     bool
    totp_secret: str = ""
    keys:        dict = field(default_factory=dict)   # {产品名: key}
    message:     str = ""
    ts:          str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _emit(cb: ProgressCallback, msg: str):
    log.info(msg)
    if cb:
        cb(msg)


def _persist_result(result: KeyResult):
    """追加写入 JSONL 结果文件（含密码，方便后续重新导出）。"""
    os.makedirs(_RESULTS_DIR, exist_ok=True)
    fpath = os.path.join(_RESULTS_DIR, f"keys_{datetime.now().strftime('%Y%m%d')}.jsonl")
    try:
        entry = {
            "ts":          result.ts,
            "email":       result.account.email,
            "password":    result.account.password,
            "success":     result.success,
            "totp_secret": result.totp_secret,
            "keys":        result.keys,
            "message":     result.message,
        }
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"写入结果失败: {e}")


class DynamicProxyBridge:
    """全功能动态本地代理桥接器：
    支持 Chromium 零重启无感热切换上游代理 (SOCKS5 / SOCKS4 / HTTP / Direct)。
    """
    def __init__(self, initial_proxy: str | None = None):
        import socket, threading, socketserver
        import socks as _socks
        self._socket = socket
        self._threading = threading
        self._socketserver = socketserver
        self._socks_mod = _socks
        self._lock = threading.Lock()
        self._active_sockets = set()
        self._server = None
        self.local_port = None
        self._xray_chain = None
        self._total_bytes_transferred = 0
        self._set_upstream_locked(initial_proxy)

    def record_bytes(self, num_bytes: int):
        if num_bytes > 0:
            with self._lock:
                self._total_bytes_transferred += num_bytes

    def get_total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes_transferred

    def get_total_mb(self) -> float:
        with self._lock:
            return self._total_bytes_transferred / (1024 * 1024)

    def _set_upstream_locked(self, proxy_str: str | None):
        self._raw_proxy = proxy_str
        if not proxy_str:
            self._scheme = "direct"
            self._host = None
            self._port = None
            self._user = None
            self._pwd = None
            return

        p = urlparse(proxy_str)
        self._scheme = (p.scheme or "http").lower()
        self._host = p.hostname
        self._port = p.port or (1080 if "socks" in self._scheme else 80)
        self._user = p.username
        self._pwd = p.password

    def switch_upstream(self, new_proxy: str | None):
        """热切换上游代理并主动断开旧连接。"""
        with self._lock:
            self._set_upstream_locked(new_proxy)
            sockets_to_close = list(self._active_sockets)
        
        for s in sockets_to_close:
            try:
                s.close()
            except Exception:
                pass

    def get_current_proxy(self) -> str | None:
        with self._lock:
            return self._raw_proxy

    def start(self) -> int:
        bridge = self
        socket = self._socket
        threading = self._threading
        ss = self._socketserver

        class Handler(ss.BaseRequestHandler):
            def handle(self):
                client_sock = self.request
                upstream_sock = None
                with bridge._lock:
                    bridge._active_sockets.add(client_sock)
                    scheme = bridge._scheme
                    host = bridge._host
                    port = bridge._port
                    user = bridge._user
                    pwd = bridge._pwd

                try:
                    # 读取 HTTP / CONNECT 请求
                    raw = b""
                    while b"\r\n\r\n" not in raw:
                        chunk = client_sock.recv(4096)
                        if not chunk:
                            return
                        raw += chunk
                        if len(raw) > 65536:
                            return

                    bridge.record_bytes(len(raw))
                    first_line = raw.split(b"\r\n")[0].decode(errors="ignore")
                    parts = first_line.split()
                    if len(parts) < 2:
                        return

                    method = parts[0].upper()
                    target = parts[1]

                    if method == "CONNECT":
                        t_host, _, t_port_s = target.rpartition(":")
                        t_port = int(t_port_s) if t_port_s else 443
                    else:
                        p_target = urlparse(target)
                        t_host = p_target.hostname or target
                        t_port = p_target.port or 80

                    # 向上游建立连接
                    if scheme in ("socks5", "socks5h", "socks4", "socks4a") and host:
                        stype = bridge._socks_mod.SOCKS4 if "4" in scheme else bridge._socks_mod.SOCKS5
                        sock = bridge._socks_mod.socksocket()
                        sock.set_proxy(stype, host, port, True, user, pwd)
                        sock.settimeout(30)
                        sock.connect((t_host, t_port))
                        upstream_sock = sock
                    elif scheme in ("http", "https") and host:
                        import base64
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(30)
                        sock.connect((host, port))
                        connect_req = f"CONNECT {t_host}:{t_port} HTTP/1.1\r\nHost: {t_host}:{t_port}\r\n"
                        if user and pwd:
                            auth_str = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                            connect_req += f"Proxy-Authorization: Basic {auth_str}\r\n"
                        connect_req += "\r\n"
                        sock.sendall(connect_req.encode())
                        resp = b""
                        while b"\r\n\r\n" not in resp:
                            c = sock.recv(4096)
                            if not c:
                                break
                            resp += c
                        if b" 200 " not in resp.split(b"\r\n")[0]:
                            sock.close()
                            return
                        upstream_sock = sock
                    else:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(30)
                        sock.connect((t_host, t_port))
                        upstream_sock = sock

                    with bridge._lock:
                        bridge._active_sockets.add(upstream_sock)

                    if method == "CONNECT":
                        client_sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    else:
                        upstream_sock.sendall(raw)

                    def relay(src, dst):
                        try:
                            while True:
                                data = src.recv(65536)
                                if not data:
                                    break
                                bridge.record_bytes(len(data))
                                dst.sendall(data)
                        except Exception:
                            pass
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except Exception:
                            pass

                    t1 = threading.Thread(target=relay, args=(client_sock, upstream_sock), daemon=True)
                    t2 = threading.Thread(target=relay, args=(upstream_sock, client_sock), daemon=True)
                    t1.start()
                    t2.start()
                    t1.join()
                    t2.join()

                except Exception as e:
                    try:
                        client_sock.sendall(f"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\nProxy Error: {e}".encode())
                    except Exception:
                        pass
                finally:
                    with bridge._lock:
                        bridge._active_sockets.discard(client_sock)
                        if upstream_sock:
                            bridge._active_sockets.discard(upstream_sock)
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                    if upstream_sock:
                        try:
                            upstream_sock.close()
                        except Exception:
                            pass

        class _Server(ss.ThreadingMixIn, ss.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        srv = _Server(("127.0.0.1", 0), Handler)
        self.local_port = srv.server_address[1]
        self._server = srv
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return self.local_port

    def stop(self):
        with self._lock:
            active = list(self._active_sockets)
        for s in active:
            try:
                s.close()
            except Exception:
                pass
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._xray_chain:
            try:
                self._xray_chain.stop()
            except Exception:
                pass
            self._xray_chain = None


class ProxyController:
    """代理热切换控制器：支持在页面卡顿/超时时无需重启浏览器动态轮换上游代理。"""
    def __init__(self, bridge: DynamicProxyBridge | None = None, rotator: ProxyRotator | None = None):
        self.bridge = bridge
        self.rotator = rotator

    async def switch_next(self) -> str | None:
        """热切换到下一个代理，返回新代理地址字符串。"""
        if not self.bridge or not self.rotator:
            return None
        next_proxy, _ = await self.rotator.get_next_proxy()
        if next_proxy:
            self.bridge.switch_upstream(next_proxy)
            return next_proxy
        return None


def _build_proxy_config(proxy_str: str | None, vless_str: str | None):
    """构建 Playwright proxy 配置，统一使用 DynamicProxyBridge 实现零重启热切换。"""
    if not proxy_str and not vless_str:
        return None, None

    if vless_str and proxy_str:
        chain = XrayProxyChain(vless_str, proxy_str)
        local_url = chain.start()
        bridge = DynamicProxyBridge(local_url)
        bridge._xray_chain = chain
        local_port = bridge.start()
        return {"server": f"http://127.0.0.1:{local_port}"}, bridge

    bridge = DynamicProxyBridge(proxy_str)
    local_port = bridge.start()
    return {"server": f"http://127.0.0.1:{local_port}"}, bridge





# ── 单账号处理 ────────────────────────────────────────────────
async def process_account(
    account: Account,
    headless: bool = False,
    proxy_str: str | None = None,
    vless_str: str | None = None,
    cb: ProgressCallback = None,
    mode: str = "azure_student",
    raise_network_retry: bool = True,
    proxy_rotator: ProxyRotator | None = None,
) -> KeyResult:
    """处理单个账号，返回 KeyResult。支持 azure_student 与 direct_ms 两种模式。"""
    _emit(cb, f"\n{'═'*50}")
    _emit(cb, f"🚀 开始处理: {account.email} (模式: {'微软账号直提' if mode == 'direct_ms' else 'Azure学生认证'})")
    _emit(cb, f"{'═'*50}")

    proxy_config, proxy_obj = None, None
    proxy_ctrl = None
    pw = None
    browser = None
    driver_pid = None

    try:
        # 构建代理配置
        if proxy_str or vless_str:
            proxy_config, proxy_obj = _build_proxy_config(proxy_str, vless_str)
            if isinstance(proxy_obj, DynamicProxyBridge):
                proxy_ctrl = ProxyController(bridge=proxy_obj, rotator=proxy_rotator)
            _emit(cb, f"  🌐 代理: {'VLESS链式' if vless_str else proxy_str}")
        else:
            _emit(cb, "  🖥️  直连模式（无代理）")

        # 启动浏览器
        pw = await async_playwright().start()
        driver_pid = get_playwright_driver_pid(pw)
        BrowserProcessManager.register_driver(driver_pid)

        chromium_path = config.get_chromium_path()
        exe_path = chromium_path if chromium_path and os.path.exists(chromium_path) else None

        browser, ctx, fp, stats = await new_fingerprint_context(
            pw, headless=headless,
            proxy_config=proxy_config,
            exe_path=exe_path,
            slow_mo=config.SLOW_MO,
            enable_save_data=config.ENABLE_SAVE_DATA,
        )
        if stats and proxy_obj and hasattr(stats, "set_proxy_bridge"):
            stats.set_proxy_bridge(proxy_obj)
        BrowserProcessManager.record_descendants(driver_pid)
        c_ver = fp.get('chrome_ver', fp.get('major_ver', '152'))
        vp_info = fp.get('viewport', {'width': 1920, 'height': 1080})
        tz_info = fp.get('timezone', 'UTC')
        _emit(cb, f"  🖥️  指纹: Chrome/{c_ver} | {vp_info['width']}x{vp_info['height']} | TZ={tz_info}")
        if config.ENABLE_SAVE_DATA:
            _emit(cb, "  🚀 省流量模式已开启（已自动拦截图片、媒体、字体与遥测数据）")
        else:
            _emit(cb, "  🌐 全量资源模式已开启（无拦截，完整下载 Angular 前端、字体及所有依赖资源）")

        page = await ctx.new_page()
        page.set_default_timeout(config.ELEMENT_TIMEOUT)
        try:
            vp_w = fp.get('viewport', {}).get('width', 1920)
            vp_h = fp.get('viewport', {}).get('height', 1080)
            await page.mouse.move(
                random.randint(int(vp_w * 0.3), int(vp_w * 0.7)),
                random.randint(int(vp_h * 0.25), int(vp_h * 0.65)),
                steps=random.randint(3, 6)
            )
        except Exception:
            pass

        if mode == "direct_ms":
            # ── 微软账号直提模式：跳过学生认证填表，直接登录 Education Software 页面 ──
            _emit(cb, "  ⚡ 模式: 微软账号 Key 直提（免 Azure 认证）")
            totp_secret = await do_azure_login(
                page, ctx, account.email, account.password, cb,
                target_url=config.AZURE_EDU_SOFTWARE_URL,
                existing_totp_secret=account.totp_secret,
                proxy_ctrl=proxy_ctrl,
            )
            _emit(cb, "  🔑 登录就绪，直接提取 6 个目标产品的 Key...")
            keys, totp_secret = await extract_all_keys(page, ctx, totp_secret, account.email, cb, proxy_str=proxy_str, proxy_ctrl=proxy_ctrl)
            if totp_secret and not account.totp_secret:
                account.totp_secret = totp_secret
                _emit(cb, f"  💾 成功捕获并保存 2FA 密钥: {totp_secret}")
            got_keys = {k: v for k, v in keys.items() if v}
            failed_keys = [k for k, v in keys.items() if not v]
            _emit(cb, f"\n  📊 提取结果: {len(got_keys)}/{len(config.PRODUCTS_TO_EXTRACT)} 个 key")
            for prod, key in got_keys.items():
                _emit(cb, f"    ✅ {prod[:40]}: {key}")
            for prod in failed_keys:
                _emit(cb, f"    ❌ {prod[:40]}: 未获取")
            success = len(got_keys) > 0
            msg = f"获取 {len(got_keys)}/{len(config.PRODUCTS_TO_EXTRACT)} 个 key"
            if failed_keys:
                msg += f"，失败: {', '.join(p[:20] for p in failed_keys)}"
            result = KeyResult(account=account, success=success,
                               totp_secret=totp_secret, keys=keys, message=msg)
            _persist_result(result)
            return result

        # ── 步骤1: Azure 登录 (Azure 学生认证模式) ─────────────
        totp_secret = await do_azure_login(
            page, ctx, account.email, account.password, cb,
            target_url=getattr(config, "AZURE_SIGNUP_LOGIN_URL", config.AZURE_SIGNUP_URL),
            existing_totp_secret=account.totp_secret,
            proxy_ctrl=proxy_ctrl,
        )
        if totp_secret and not account.totp_secret:
            account.totp_secret = totp_secret
            _emit(cb, f"  💾 成功捕获并保存 2FA 密钥: {totp_secret}")

        # ── 步骤2: 确保主动定向到学生认证入口 ──────────────
        # 必须带上 returnUrl 到 studentverification，使微软账号上下文与学生认证严格绑定
        if "studentverification" not in page.url.lower() and "signup?offer=" not in page.url.lower() and "portal.azure.com" not in page.url.lower():
            target_redir = config.AZURE_SIGNUP_LOGIN_URL
            _emit(cb, f"  🌐 主动定向至学生认证入口: {target_redir[:80]}")
            try:
                await page.goto(target_redir, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                _emit(cb, f"  ⚠️ 导航学生认证入口异常: {e}")

        # 每1秒检测一次，直到出现关键状态字符（最多等3分钟）
        _emit(cb, "  🔍 检测页面状态（每秒轮询，支持卡顿自动刷新）...")
        page_title = ""
        page_headings = ""
        current_url = page.url
        is_already_registered = False
        is_unregistered = False

        last_state_snapshot = ""
        last_change_time = asyncio.get_event_loop().time()
        refresh_count = 0

        for poll_i in range(180):
            current_url = page.url

            # 实时检测是否触发人机拼图验证码
            is_captcha, cap_msg = await check_captcha_present(page)
            if is_captcha:
                raise AzureCaptchaError(f"触发 Azure 人机拼图验证 ({cap_msg})")

            # portal / education 平台直接判定为已注册
            if "portal.azure.com" in current_url or "education.azure.com" in current_url or "azureforeducation" in current_url:
                is_already_registered = True
                _emit(cb, f"  ✅ 检测到 Azure 业务管理平台（{poll_i+1}s）: {current_url[:60]}")
                break

            # 优先通过 DOM 表单节点高频检测未注册 SheerID 学术表单（穿透所有 Frame）
            try:
                targets = [page] + list(page.frames)
                for tgt in targets:
                    fname_loc = tgt.locator(
                        "#sse-vnext-fname-input, #first-name-input, input[id*='fname' i], input[placeholder*='First' i], "
                        "button:has-text('Verify academic status'), button:has-text('验证学术状态'), "
                        "#sse-vnext-submit-button"
                    ).first
                    if await fname_loc.is_visible(timeout=80):
                        is_unregistered = True
                        _emit(cb, f"  ✅ 检测到 SheerID 学术认证表单节点（{poll_i+1}s）")
                        break
                if is_unregistered:
                    break
            except Exception:
                pass

            try:
                info = await page.evaluate("""
                    () => {
                        const title = document.title || '';
                        const headings = [...document.querySelectorAll('h1,h2,h3')]
                            .map(el => el.innerText.trim())
                            .filter(t => t.length > 0 && t.length < 200)
                            .join(' | ');
                        const bodyText = (document.body ? document.body.innerText : '').slice(0, 3000);
                        return { title, headings, bodyText };
                    }
                """)
                page_title    = info.get("title", "").lower()
                page_headings = info.get("headings", "").lower()
                body_sample   = info.get("bodyText", "").lower()
            except Exception:
                page_title = ""
                page_headings = ""
                body_sample = ""

            combined = f"{page_title} {page_headings} {body_sample}"

            # 检查页面是否有推进（在 studentverification 确认中时允许更长等待，避免打断 Angular 异步通信）
            is_confirming = any(k in combined.lower() for k in ("正在确认", "confirming", "setting up", "正在设置"))
            current_snapshot = f"{current_url}|{combined[:200]}"
            if current_snapshot != last_state_snapshot:
                last_state_snapshot = current_snapshot
                last_change_time = asyncio.get_event_loop().time()
            else:
                stagnant = asyncio.get_event_loop().time() - last_change_time
                max_stagnant = 120 if is_confirming else (30 if "studentverification" in current_url else 45)
                if stagnant >= max_stagnant and refresh_count < 3:
                    refresh_count += 1
                    last_change_time = asyncio.get_event_loop().time()
                    switched_p = None
                    if proxy_ctrl:
                        switched_p = await proxy_ctrl.switch_next()
                    p_msg = f"，已热切换至代理: {switched_p}" if switched_p else ""
                    _emit(cb, f"  ⚠️ [网络延迟适配] 界面 {int(stagnant)} 秒无响应{p_msg}，重新加载学生认证页面 ({refresh_count}/3)...")
                    try:
                        await page.goto(config.AZURE_SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
                    except Exception as e:
                        _emit(cb, f"  ⚠️ 重新导航异常: {e}")

            # 已注册就绪标志 (必须已脱离 studentverification 注册入口，或直接在 Portal 中)
            if "portal.azure.com" in current_url or (
                "studentverification" not in current_url and any(k in combined for k in (
                    "get started", "overview", "welcome", "概览", "欢迎", "开始使用"
                ))
            ):
                is_already_registered = True
                _emit(cb, f"  ✅ 检测到已注册就绪状态（{poll_i+1}s）")
                break

            # 检测 SheerID 学术核验阶段 或 Your profile 个人资料阶段
            if "signup?offer=" in current_url.lower() or any(k in (page_headings + " " + page_title.lower()) for k in (
                "academic verification", "verify your academic status", "check your info",
                "学术验证", "验证学术状态", "学生验证", "your profile", "你的个人资料",
                "面向学生的 azure", "azure for students"
            )):
                is_unregistered = True
                _emit(cb, f"  ✅ 检测到需填写学生认证表单（{poll_i+1}s）")
                break

            # 停留超过 20 秒且在 signup.azure.com 页面：判定为待认证学生账号，转入填表
            if poll_i >= 20 and "signup.azure.com" in current_url and "portal.azure.com" not in current_url:
                is_unregistered = True
                _emit(cb, f"  ✅ 页面停留在 Azure 学生注册入口，转入表单自适应填写（{poll_i+1}s）")
                break

            if poll_i % 10 == 0 and poll_i > 0:
                _emit(cb, f"  ⏳ [{poll_i}s] 等待认证页面跳转... url={current_url[:60]}")

            await asyncio.sleep(1)

        _emit(cb, f"  🔍 URL: {current_url[:80]}")
        _emit(cb, f"  🔍 Title: {page_title}")
        _emit(cb, f"  🔍 Headings: {page_headings[:150]}")

        if is_already_registered:
            _emit(cb, "  ℹ️  账号已完成注册，直接提取 key")
            keys, totp_secret = await extract_all_keys(page, ctx, totp_secret, account.email, cb, proxy_str=proxy_str, proxy_ctrl=proxy_ctrl)
            if totp_secret and not account.totp_secret:
                account.totp_secret = totp_secret
                _emit(cb, f"  💾 成功捕获并保存 2FA 密钥: {totp_secret}")
            got_keys = {k: v for k, v in keys.items() if v}
            failed_keys = [k for k, v in keys.items() if not v]
            _emit(cb, f"\n  📊 提取结果: {len(got_keys)}/{len(config.PRODUCTS_TO_EXTRACT)} 个 key")
            for prod, key in got_keys.items():
                _emit(cb, f"    ✅ {prod[:40]}: {key}")
            for prod in failed_keys:
                _emit(cb, f"    ❌ {prod[:40]}: 未获取")
            success = len(got_keys) > 0
            msg = f"获取 {len(got_keys)}/{len(config.PRODUCTS_TO_EXTRACT)} 个 key"
            if failed_keys:
                msg += f"，失败: {', '.join(p[:20] for p in failed_keys)}"
            result = KeyResult(account=account, success=success,
                               totp_secret=totp_secret, keys=keys, message=msg)
            _persist_result(result)
            return result

        else:
            # 仅执行第一阶段 SheerID 学术资格核验
            _emit(cb, "  📝 执行 SheerID 学术资格认证...")
            await fill_azure_profile_form(page, account.email, cb)

        # ── 步骤3: 新开标签页提取所有产品 key ────────────────
        # 直接调用 extract_all_keys，新开标签页直接导航到 software 页面
        _emit(cb, "  🌐 开始提取 key（新开标签页）...")
        keys, totp_secret = await extract_all_keys(page, ctx, totp_secret, account.email, cb, proxy_str=proxy_str, proxy_ctrl=proxy_ctrl)
        if totp_secret and not account.totp_secret:
            account.totp_secret = totp_secret
            _emit(cb, f"  💾 成功捕获并保存 2FA 密钥: {totp_secret}")

        # 统计结果
        got_keys = {k: v for k, v in keys.items() if v}
        failed_keys = [k for k, v in keys.items() if not v]

        _emit(cb, f"\n  📊 提取结果: {len(got_keys)}/{len(config.PRODUCTS_TO_EXTRACT)} 个 key")
        for prod, key in got_keys.items():
            _emit(cb, f"    ✅ {prod[:40]}: {key}")
        for prod in failed_keys:
            _emit(cb, f"    ❌ {prod[:40]}: 未获取")

        success = len(got_keys) > 0
        msg = f"获取 {len(got_keys)}/{len(config.PRODUCTS_TO_EXTRACT)} 个 key"
        if failed_keys:
            msg += f"，失败: {', '.join(p[:20] for p in failed_keys)}"

        result = KeyResult(
            account=account,
            success=success,
            totp_secret=totp_secret,
            keys=keys,
            message=msg,
        )

    except (EmailInputTimeoutError, LoginNetworkError, SheerIDVerificationError, AzureCaptchaError) as e:
        saved_totp = totp_secret if ('totp_secret' in locals() and totp_secret) else getattr(account, 'totp_secret', '')
        if saved_totp and not account.totp_secret:
            account.totp_secret = saved_totp
        if raise_network_retry:
            raise
        _emit(cb, f"  ❌ 处理异常: {e}")
        if saved_totp:
            _emit(cb, f"  💾 已保存当前账号 2FA 密钥: {saved_totp}")
        result = KeyResult(
            account=account,
            success=False,
            totp_secret=saved_totp,
            message=str(e)[:200],
        )
    except Exception as e:
        _emit(cb, f"  ❌ 处理异常: {e}")
        saved_totp = totp_secret if ('totp_secret' in locals() and totp_secret) else getattr(account, 'totp_secret', '')
        if saved_totp and not account.totp_secret:
            account.totp_secret = saved_totp
        if saved_totp:
            _emit(cb, f"  💾 已保存当前账号 2FA 密钥: {saved_totp}")
        result = KeyResult(
            account=account,
            success=False,
            totp_secret=saved_totp,
            message=str(e)[:200],
        )

    finally:
        if 'stats' in locals() and stats:
            _emit(cb, f"  📊 本次账号真实网络消耗约 {stats.get_transfer_mb():.2f} MB | 本地强缓存为您节省 {stats.get_cache_saved_mb():.2f} MB（命中强缓存 {stats.cache_hit_count} 次，拦截非必要请求 {stats.blocked_count} 个）")
            if hasattr(stats, "get_top_downloads"):
                top_dl = stats.get_top_downloads(5)
                if top_dl:
                    _emit(cb, "  🔝 本次直连网络请求 Top 5:")
                    for sz_kb, url, r_type in top_dl:
                        if sz_kb >= 1024:
                            _emit(cb, f"    • {sz_kb/1024:.2f} MB [{r_type}] {url[:85]}")
                        else:
                            _emit(cb, f"    • {sz_kb:.1f} KB [{r_type}] {url[:85]}")
        
        # 安全优雅关闭（使用 shield 保护不受取消中断）+ 强制杀死所有底层浏览器/驱动子进程
        await safe_close_playwright(
            browser=browser,
            ctx=ctx if 'ctx' in locals() else None,
            pw=pw,
            driver_pid=driver_pid,
            timeout=3.0,
        )

        if proxy_obj:
            try:
                proxy_obj.stop()
            except Exception:
                pass

    # 持久化结果
    _persist_result(result)
    return result


# ── 批量流水线 ────────────────────────────────────────────────
async def run_pipeline(
    accounts: list[Account],
    headless: bool = False,
    proxy_str: str | None = None,
    vless_str: str | None = None,
    cb: ProgressCallback = None,
    account_timeout: int = 900,
    on_result: object = None,
    concurrency: int = 1,
    mode: str = "azure_student",
    batch_index: int = 0,
    total_batches: int = 1,
    overall_offset: int = 0,
    overall_total: int | None = None,
) -> list[KeyResult]:
    """
    批量流水线（支持多进程/多协程并发与代理池轮询分发）。
    mode: 'azure_student' (学生认证+提取) 或 'direct_ms' (微软账号直提)。
    concurrency: 同时并发运行的账号数（默认1，建议2-5）。
    account_timeout: 单账号最大耗时（秒），默认 15 分钟。
    on_result: 单账号完成后的即时回调函数。
    """
    if not accounts:
        return []

    # 解析代理池列表
    proxy_pool = config.parse_proxy_list(proxy_str)
    rotator = ProxyRotator(proxy_pool)
    total = len(accounts)
    concurrency = max(1, min(int(concurrency), 20))

    if proxy_pool:
        _emit(cb, f"🌐 代理池构建完成: 共 {len(proxy_pool)} 个代理节点，将按轮询方式分发给各并发进程")
    else:
        _emit(cb, "🖥️ 代理池为空，将使用直连模式处理")

    batch_tag = f"[批次 {batch_index + 1}/{total_batches}] " if total_batches > 1 else ""
    _emit(cb, f"⚡ {batch_tag}并发限制已设为: {concurrency} 个任务同步进行，当前模式: {'微软账号直提' if mode == 'direct_ms' else 'Azure学生认证'}")

    sem = asyncio.Semaphore(concurrency)
    stop_pipeline_event = asyncio.Event()
    results: list[KeyResult] = []
    results_lock = asyncio.Lock()

    async def _process_single_account(idx: int, account: Account):
        if stop_pipeline_event.is_set():
            return None

        async with sem:
            if stop_pipeline_event.is_set():
                return None

            prefix = f"[{account.email.split('@')[0]}]"

            # 自定义打日志回调，为多并发日志添加前缀标识
            def _account_cb(msg: str):
                _emit(cb, f"{prefix} {msg}")

            curr_proxy, _ = await rotator.get_next_proxy()
            proxy_info = f" | 代理: {curr_proxy}" if curr_proxy else " | 直连"
            display_idx = (overall_offset + idx + 1) if overall_total else (idx + 1)
            display_total = overall_total if overall_total else total
            batch_prefix = f"[批次 {batch_index + 1}/{total_batches}] " if total_batches > 1 else ""
            _emit(cb, f"\n{batch_prefix}{prefix} 🚀 启动任务 ({display_idx}/{display_total}){proxy_info}")
            _emit(cb, f"[{display_idx}/{display_total}] 处理账号: {account.email} 开始处理: {account.email}")

            result = None
            max_login_retries = 1
            for attempt in range(1 + max_login_retries):
                try:
                    result = await asyncio.wait_for(
                        process_account(
                            account,
                            headless=headless,
                            proxy_str=curr_proxy,
                            vless_str=vless_str,
                            cb=_account_cb,
                            mode=mode,
                            raise_network_retry=True,
                            proxy_rotator=rotator,
                        ),
                        timeout=account_timeout,
                    )
                    break
                except (SheerIDVerificationError, AzureCaptchaError) as e:
                    saved_totp = getattr(account, 'totp_secret', '')
                    totp_msg = f"，已留存 2FA 密钥 ({saved_totp[:6]}...)" if saved_totp else ""
                    if attempt < max_login_retries:
                        curr_proxy, _ = await rotator.get_next_proxy()
                        retry_proxy_info = f"代理: {curr_proxy}" if curr_proxy else "直连"
                        _account_cb(f"⚠️ [新开浏览器重试] {e}，已关闭旧浏览器{totp_msg}，正在启动全新浏览器重试 (第 {attempt+2}/{1+max_login_retries} 次) | {retry_proxy_info}...")
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        _account_cb(f"❌ 全新浏览器重试后仍未通过: {e}")
                        result = KeyResult(
                            account=account, success=False, totp_secret=saved_totp,
                            message=f"新开浏览器重试仍失败: {e}"[:200]
                        )
                        _persist_result(result)
                        break
                except (EmailInputTimeoutError, LoginNetworkError) as e:
                    err_msg = str(e)
                    is_tunnel_err = any(k in err_msg for k in ["ERR_TUNNEL_CONNECTION_FAILED", "ERR_EMPTY_RESPONSE", "ERR_PROXY", "502 Bad Gateway"])
                    if is_tunnel_err and curr_proxy:
                        _account_cb(f"⚠️  [代理隧道断开/认证失败] 当前代理 {curr_proxy} 拒绝连接或无法访问目标网站！")
                    if attempt < max_login_retries:
                        curr_proxy, _ = await rotator.get_next_proxy()
                        retry_proxy_info = f"代理: {curr_proxy}" if curr_proxy else "直连"
                        _account_cb(f"⚠️ [网络/代理异常] 邮箱登录页面未加载或输入失败 ({e})，正在切换至下一个代理重试 (1/1) | {retry_proxy_info}...")
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        if is_tunnel_err and curr_proxy:
                            _account_cb(f"❌ 代理连接失败: 当前代理池所有节点均无法建立连接 (SOCKS5 认证拒绝/流量耗尽)，请检查代理套餐！")
                        else:
                            _account_cb(f"❌ 切换代理重试后仍无法输入邮箱或加载页面: {e}")
                        result = KeyResult(
                            account=account, success=False, message=f"重试仍失败: {e}"[:200]
                        )
                        _persist_result(result)
                        break
                except asyncio.TimeoutError:
                    _emit(cb, f"{prefix} ⏰ 账号处理超时（>{account_timeout}s），已放弃")
                    result = KeyResult(
                        account=account, success=False,
                        message=f"超时跳过（>{account_timeout}s）"
                    )
                    _persist_result(result)
                    break
                except asyncio.CancelledError:
                    _emit(cb, f"{prefix} ⚠️ 任务被取消")
                    raise
                except Exception as e:
                    _emit(cb, f"{prefix} ❌ 账号处理崩溃: {e}")
                    result = KeyResult(
                        account=account, success=False, message=str(e)[:200]
                    )
                    _persist_result(result)
                    break

            if result is not None:
                async with results_lock:
                    results.append(result)

                if on_result and callable(on_result):
                    try:
                        on_result(result)
                    except Exception:
                        pass

                # 检测学校封控——标志熔断停止
                if result.message and "学校已被微软封控" in result.message:
                    _emit(cb, f"\n🚫 [{account.email}] 检测到学校封控，停止后续任务")
                    stop_pipeline_event.set()

            return result

    # 并发并发任务组
    tasks = [asyncio.create_task(_process_single_account(i, acc)) for i, acc in enumerate(accounts)]
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except asyncio.CancelledError:
        _emit(cb, "⚠️ 批量并发任务响应中止指令，正在清理退出...")
        for t in tasks:
            if not t.done():
                t.cancel()
        raise
    finally:
        # 全量清理所有可能残留的浏览器与驱动进程
        BrowserProcessManager.cleanup_all()

    return results
