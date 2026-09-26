"""
Azure Education Software Key 提取模块
---------------------------------------
流程:
  1. 访问 portal.azure.com Education Software 页面
  2. 若出现「Terms Acceptance」→ 勾选 checkbox → 点「Accept and continue」
  3. 等待软件列表加载
  4. 对每个目标产品：搜索 → 点击产品名 → 点「View Key」→ 提取 key
  5. 返回 dict {产品名: key}
"""

import asyncio
import base64
import logging
import os
import random
import re
import sys
from typing import Callable, Optional, Any

import pyotp
from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

log = logging.getLogger(__name__)
ProgressCallback = Optional[Callable[[str], None]]

# 目标产品搜索关键词映射（搜索词 → 精确产品名）
_PRODUCT_SEARCH_MAP = {
    "Visio Professional 2021": "Visio",
    "Project Professional 2021 - DVD": "Project",
    "Windows Server 2022 Standard (updated July 2023)": "Windows Server 2022",
    "Windows Server 2025 Standard": "Windows Server 2025",
    "Windows Server 2019 Standard (updated Mar 2023)": "Windows Server 2019",
    "Windows 11 Education, version 25H2": "Windows 11 Education",
}


def _emit(cb: ProgressCallback, msg: str):
    log.info(msg)
    if cb:
        cb(msg)


async def _wait_for_portal_ready(page: Page, cb: ProgressCallback, timeout: int = 120,
                                   totp_secret: str = "", ms_email: str = "", proxy_ctrl: Optional[Any] = None) -> tuple[bool, str]:
    """等待 Azure Portal 完全加载（出现导航栏或搜索框）。
    在等待过程中实时感知 2FA / 登录重定向并自动输入 TOTP 验证码。
    如果超过 2 分钟没响应，强制重新导航到 Education Software 页面。
    """
    _EDU_SW_URL = (
        "https://portal.azure.com/#view/Microsoft_Azure_Education"
        "/EducationMenuBlade/~/software"
    )
    _emit(cb, "  ⏳ 等待 Azure Portal 加载...")
    start_time = asyncio.get_event_loop().time()
    deadline = start_time + timeout
    retry_done = False
    current_totp = totp_secret

    while asyncio.get_event_loop().time() < deadline:
        cur_url = (page.url or "").lower()

        # 1. 实时检测是否触发微软登录/OAuth/2FA 验证页 (如 login.microsoftonline.com/organizations/oauth2/...)
        if any(k in cur_url for k in ("login.microsoftonline", "login.live.com", "mysignins.microsoft.com")) or await _is_mfa_login_prompt(page):
            _emit(cb, "  🔑 检测到 Portal 登录/2FA 重定向，正在自动处理...")
            new_totp = await _handle_portal_login(page, current_totp, ms_email, cb)
            if new_totp:
                current_totp = new_totp
            await asyncio.sleep(1)
            continue

        # 2. 检测 Portal 是否已真正渲染就绪 (排除 bare skeleton)
        try:
            ready = await page.evaluate("""
                () => {
                    const is_login = window.location.href.includes('login.microsoftonline') ||
                                     window.location.href.includes('login.live.com') ||
                                     window.location.href.includes('mysignins');
                    if (is_login) return false;
                    return !!(
                        document.querySelector('[placeholder*="Search"]') ||
                        document.querySelector('[aria-label*="Search"]') ||
                        document.querySelector('.fxs-topbar') ||
                        document.querySelector('[class*="topbar"]') ||
                        document.querySelector('input[type="search"]') ||
                        document.querySelector('[class*="education"]') ||
                        document.querySelector('[class*="software"]') ||
                        (document.body.innerText || '').includes('Education') ||
                        (document.body.innerText || '').includes('Software')
                    );
                }
            """)
            if ready:
                _emit(cb, "  ✅ Azure Portal 已加载")
                return True, current_totp
        except Exception:
            pass

        # 超过 2 分钟没响应，强制重新导航（只重试一次）
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > 120 and not retry_done:
            retry_done = True
            _emit(cb, "  ⚠️  Portal 加载超过 2 分钟，强制重新导航...")
            try:
                await page.goto(_EDU_SW_URL, wait_until="domcontentloaded", timeout=90000)
                _emit(cb, "  🔄 已重新导航到 Education Software 页面")
            except Exception as e:
                _emit(cb, f"  ⚠️  重新导航异常: {e}")
            await asyncio.sleep(3)

            # 重新导航后可能触发 MFA 登录流程，用已有 secret 处理
            _emit(cb, "  🔍 检查是否需要重新 MFA...")
            new_totp = await _handle_portal_login(page, current_totp, ms_email, cb)
            if new_totp:
                current_totp = new_totp
            continue

        await asyncio.sleep(1)

    _emit(cb, "  ⚠️  Portal 加载超时，尝试继续...")
    return False, current_totp


async def _is_mfa_login_prompt(page: Page) -> bool:
    """检测当前页面是否包含 2FA 验证码输入框或 MFA / 登录相关特征。"""
    try:
        return await page.evaluate("""
            () => {
                const sels = [
                    "input[name='otc']", "input#idTxtBx_SAOTCC_OTC",
                    "input[autocomplete='one-time-code']",
                    "input[placeholder*='code' i]", "input[placeholder*='代码' i]"
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && (el.offsetWidth > 0 || el.offsetHeight > 0)) return true;
                }
                const txt = (document.body ? document.body.innerText : '').toLowerCase();
                return txt.includes('enter code') || txt.includes('enter the code') ||
                       txt.includes('set up your account in app') || txt.includes("let's keep your account secure") ||
                       txt.includes("install microsoft authenticator") || txt.includes("start by getting the app") ||
                       txt.includes("scan the qr code") || txt.includes("more information required") ||
                       txt.includes("keep your account secure") || txt.includes("setting up your passkey");
            }
        """)
    except Exception:
        return False



async def _search_product(page: Page, search_term: str, cb: ProgressCallback) -> bool:
    """
    在 Education Software 内部搜索框输入产品名。
    关键：排除顶部全局搜索框（'Search resources, services, and docs'）。
    支持多框架穿透 + 彻底清空旧搜索词 + 真实键盘输入与 React 状态同步。
    """
    _FIND_SEARCH_JS = """
        () => {
            const inputs = [...document.querySelectorAll('input')].filter(i => {
                const r = i.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                if (i.disabled || i.readOnly) return false;
                const ph = (i.placeholder || '').toLowerCase();
                const al = (i.getAttribute('aria-label') || '').toLowerCase();
                if (ph.includes('search resources') || al.includes('search resources') || al.includes('global search')) return false;
                return ph.includes('search') || al.includes('search') || i.type === 'search';
            });
            if (inputs.length === 0) return false;
            inputs.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
            const target = inputs[inputs.length - 1];
            target.focus();
            target.click();
            // 先通过 JS 原生 prototype 清空旧搜索值并触发事件
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            if (setter) setter.call(target, '');
            else target.value = '';
            target.dispatchEvent(new Event('input', {bubbles: true}));
            target.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }
    """

    for attempt in range(10):  # 最多等待 5 秒
        for frame in [page] + list(page.frames):
            try:
                found = await frame.evaluate(_FIND_SEARCH_JS)
                if found:
                    _emit(cb, f"  🔍 搜索: {search_term}")
                    # 键盘全选清空旧搜索词后键入新词
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(0.08)
                    await page.keyboard.type(search_term, delay=25)
                    await asyncio.sleep(0.08)
                    await page.keyboard.press("Enter")
                    # 等待搜索结果过滤（最多 1.2 秒）
                    await asyncio.sleep(1.2)
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.5)

    _emit(cb, f"  ⚠️  搜索失败: 未能定位到软件搜索输入框 ({search_term})")
    return False


async def _click_product_row(page: Page, product_name: str, cb: ProgressCallback) -> bool:
    """点击产品列表中的产品名（蓝色链接），打开右侧详情面板。支持跨 iframe 与直接定位。"""
    await asyncio.sleep(0.3)

    candidates = []
    candidates.append(product_name)
    short = product_name.split("(")[0].split("-")[0].strip()
    candidates.append(short)
    if "visio" in product_name.lower():
        candidates.extend(["Visio Professional 2021", "Visio Professional", "Visio"])
    if "project" in product_name.lower():
        candidates.extend(["Project Professional 2021 - DVD", "Project Professional 2021", "Project Professional", "Project"])
    if "2022" in product_name:
        candidates.extend(["Windows Server 2022 Standard", "Windows Server 2022"])
    if "2025" in product_name:
        candidates.extend(["Windows Server 2025 Standard", "Windows Server 2025"])
    if "2019" in product_name:
        candidates.extend(["Windows Server 2019 Standard", "Windows Server 2019"])
    if "windows 11" in product_name.lower():
        candidates.extend(["Windows 11 Education, version 25H2", "Windows 11 Education", "Windows 11"])

    # 1. 优先使用 Playwright 原生定位器（优先点击 <a>、<button>、[role=link]，防止点到普通表格行空白处）
    for scope in [page] + list(page.frames):
        for cand in candidates:
            for selector in [
                f"a:has-text('{cand}')",
                f"button:has-text('{cand}')",
                f"[role='link']:has-text('{cand}')",
                f"[role='gridcell'] a:has-text('{cand}')",
                f"[role='gridcell'] span:has-text('{cand}')",
                f"text='{cand}'",
            ]:
                try:
                    loc = scope.locator(selector).first
                    if await loc.is_visible(timeout=300):
                        await loc.scroll_into_view_if_needed()
                        await loc.click(force=True)
                        _emit(cb, f"  ✅ 点击产品: {cand}")
                        await asyncio.sleep(1.2)
                        return True
                except Exception:
                    pass

    # 2. JS 兜底深度查找与点击
    _CLICK_ROW_JS = """
        (name) => {
            const rows = [...document.querySelectorAll('a, button, [role="link"], [role="gridcell"], span, td')];
            const visible = rows.filter(a => {
                const r = a.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            const clean = (s) => (s || '').toLowerCase().replace(/[,()\\-–—]/g, ' ').replace(/\\s+/g, ' ').trim();
            const lower = clean(name);
            const matches = visible.filter(a => {
                const raw = (a.innerText || a.textContent || '').trim();
                const t = clean(raw);
                if (!t || t.length < 3 || t.length > 250) return false;
                if (t === 'software' || t === 'learning' || t === 'roles'
                    || t === 'github' || t === 'overview' || t === 'need help?' || t === 'education') return false;
                return t === lower || t.includes(lower);
            });
            if (matches.length > 0) {
                // 优先点击 a、button、[role=link] 等可交互元素
                matches.sort((a, b) => {
                    const tagA = (a.tagName || '').toLowerCase();
                    const tagB = (b.tagName || '').toLowerCase();
                    const scoreA = (tagA === 'a' || tagA === 'button' || a.getAttribute('role') === 'link') ? 0 : 1;
                    const scoreB = (tagB === 'a' || tagB === 'button' || b.getAttribute('role') === 'link') ? 0 : 1;
                    if (scoreA !== scoreB) return scoreA - scoreB;
                    return (a.innerText || '').length - (b.innerText || '').length;
                });
                const target = matches[0];
                target.scrollIntoView({block:'center'});
                target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                target.click();
                return { ok: true, text: (target.innerText || target.textContent || '').trim().substring(0, 60) };
            }
            return { ok: false };
        }
    """

    for attempt in range(6):
        for frame in page.frames:
            for cand in candidates:
                try:
                    clicked = await frame.evaluate(_CLICK_ROW_JS, cand)
                    if isinstance(clicked, dict) and clicked.get("ok"):
                        _emit(cb, f"  ✅ 点击产品 (JS): {clicked.get('text','')}")
                        await asyncio.sleep(1.2)
                        return True
                except Exception:
                    pass
        await asyncio.sleep(0.5)

    _emit(cb, f"  ⚠️  未找到产品: {product_name[:50]}")
    return False


async def _extract_key_from_panel(page: Page, product_name: str, cb: ProgressCallback,
                                  existing_keys: set[str] | list[str] | None = None) -> str:
    """
    从右侧详情面板提取 Product key。
    特点：
      1. 穿透所有 page.frames（应对 Azure Education 跨域 iframe）
      2. 穿透所有 Shadow DOM（Fast Foundation / Fluent UI 9 现代组件）
      3. 排除已有 key 集合（existing_keys），防止旧产品面板残留在 DOM 中导致误提取
      4. 即使未捕获 View Key 按钮也绝不提前关闭，全程保持动态监控轮询
    """
    _emit(cb, f"  🔑 提取 key: {product_name[:40]}...")

    existing_set = set(k.strip().upper() for k in existing_keys if k) if existing_keys else set()

    # 全方位检查 DOM、Shadow DOM、input、textarea、属性的提取 JS
    _EXTRACT_ALL_KEYS_JS = """
        () => {
            const pattern = /[A-Za-z0-9]{5}[\\s\\u00A0]*[-–—][\\s\\u00A0]*[A-Za-z0-9]{5}[\\s\\u00A0]*[-–—][\\s\\u00A0]*[A-Za-z0-9]{5}[\\s\\u00A0]*[-–—][\\s\\u00A0]*[A-Za-z0-9]{5}[\\s\\u00A0]*[-–—][\\s\\u00A0]*[A-Za-z0-9]{5}/gi;
            const clean = (m) => m ? m.replace(/[\\s\\u00A0\\u200B]+/g, '').replace(/[–—]/g, '-').toUpperCase() : '';
            const results = new Set();

            const addMatches = (text) => {
                if (!text || typeof text !== 'string') return;
                const matches = text.match(pattern);
                if (matches) {
                    for (const m of matches) {
                        const c = clean(m);
                        if (c && c.length === 29) results.add(c);
                    }
                }
            };

            // 1. 直接 innerText 快速扫描
            try {
                if (document.body) addMatches(document.body.innerText);
            } catch(e) {}

            // 2. 递归 DOM 与 Shadow DOM 深度扫描
            function walk(node) {
                if (!node) return;

                if (node.nodeType === Node.TEXT_NODE) {
                    addMatches(node.textContent);
                    return;
                }

                if (node.nodeType === Node.ELEMENT_NODE) {
                    if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') {
                        addMatches(node.value);
                        addMatches(node.getAttribute('value'));
                    }

                    for (const attr of ['data-key', 'data-clipboard-text', 'data-value', 'aria-label', 'title']) {
                        addMatches(node.getAttribute(attr));
                    }

                    if (node.shadowRoot) {
                        walk(node.shadowRoot);
                    }
                }

                for (let child = node.firstChild; child; child = child.nextSibling) {
                    walk(child);
                }
            }

            try {
                walk(document.body || document.documentElement);
            } catch(e) {}

            // 3. 检查 5 个连续独立输入框（部分 UI 采用 5x5 分格输入框）
            try {
                const inputs = Array.from(document.querySelectorAll('input')).filter(i => {
                    const v = (i.value || '').trim();
                    return /^[A-Za-z0-9]{5}$/.test(v);
                });
                if (inputs.length >= 5) {
                    for (let i = 0; i <= inputs.length - 5; i++) {
                        const combo = inputs.slice(i, i + 5).map(x => x.value.trim().toUpperCase()).join('-');
                        if (/^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$/.test(combo)) {
                            results.add(combo);
                        }
                    }
                }
            } catch(e) {}

            return Array.from(results);
        }
    """

    # 递归查找并点击 View Key 按钮的 JS（支持 Shadow DOM 穿透与强力事件分发）
    _CLICK_VIEW_KEY_JS = """
        () => {
            const isTarget = (t) => {
                if (!t) return false;
                const lower = t.toLowerCase().replace(/[\\s\\u00A0]+/g, ' ').trim();
                return lower === 'view key' || lower === 'view license key' ||
                       lower === 'get key' || lower === 'show key' ||
                       lower === 'generate key' || lower === 'view product key' ||
                       lower === 'claim key' || lower === 'reveal key' ||
                       lower === '查看密钥' || lower === '查看产品密钥' ||
                       lower === '获取密钥' || lower === '生成密钥' ||
                       lower.includes('view key') || lower.includes('查看密钥') ||
                       lower.includes('get key') || lower.includes('获取密钥') ||
                       lower.includes('generate key') || lower.includes('生成密钥');
            };

            const triggerClick = (b) => {
                b.scrollIntoView({block:'center', behavior:'instant'});
                b.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                b.click();
            };

            function findBtn(root) {
                if (!root) return null;
                const candidates = root.querySelectorAll('button, a, [role="button"], [role="link"], input[type="button"], input[type="submit"], [tabindex="0"]');
                for (const b of candidates) {
                    const r = b.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const txt = (b.innerText || b.value || b.textContent || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim();
                    if (isTarget(txt)) {
                        return { btn: b, text: txt };
                    }
                }
                const allEls = root.querySelectorAll('*');
                for (const el of allEls) {
                    if (el.shadowRoot) {
                        const found = findBtn(el.shadowRoot);
                        if (found) return found;
                    }
                }
                return null;
            }

            const found = findBtn(document.body || document.documentElement);
            if (found) {
                triggerClick(found.btn);
                return { ok: true, text: found.text.substring(0, 60) };
            }
            return { ok: false };
        }
    """

    async def _scan_all_frames_for_key() -> str:
        for frame in page.frames:
            try:
                found_list = await frame.evaluate(_EXTRACT_ALL_KEYS_JS)
                if isinstance(found_list, list):
                    for k in found_list:
                        if k and k not in existing_set:
                            return k
            except Exception:
                pass
        return ""

    async def _click_view_key_in_any_frame() -> dict | None:
        # 1. 优先使用 Playwright 原生 Locator 穿透所有框架
        for scope in [page] + list(page.frames):
            for btn_sel in [
                "button:has-text('View Key')",
                "button:has-text('View key')",
                "button:has-text('Generate Key')",
                "button:has-text('Generate key')",
                "button:has-text('Get Key')",
                "button:has-text('Get key')",
                "button:has-text('View product key')",
                "button:has-text('查看密钥')",
                "button:has-text('生成密钥')",
                "button:has-text('获取密钥')",
                "[role='button']:has-text('View Key')",
                "[role='button']:has-text('View key')",
            ]:
                try:
                    loc = scope.locator(btn_sel).first
                    if await loc.is_visible(timeout=150):
                        await loc.scroll_into_view_if_needed()
                        await loc.click(force=True)
                        return {"ok": True, "text": btn_sel}
                except Exception:
                    pass

        # 2. JS 穿透 Shadow DOM 兜底
        for frame in page.frames:
            try:
                res = await frame.evaluate(_CLICK_VIEW_KEY_JS)
                if isinstance(res, dict) and res.get("ok"):
                    return res
            except Exception:
                pass
        return None

    # ── 第一阶段：等右侧面板渲染 + 直接尝试提取 key（很多产品打开即展示 key）──
    for attempt in range(6):
        await asyncio.sleep(0.5)
        val = await _scan_all_frames_for_key()
        if val:
            _emit(cb, f"  ✅ 提取到 key（直接可见）: {val}")
            return val

    # ── 第二阶段：查找并点击 View Key 按钮 ──────────────────────
    view_key_clicked = False
    for attempt in range(6):
        click_res = await _click_view_key_in_any_frame()
        if click_res:
            _emit(cb, f"  🔘 已点击 View Key 按钮: '{click_res.get('text')}'")
            view_key_clicked = True
            break
        await asyncio.sleep(0.5)

    if not view_key_clicked:
        _emit(cb, "  ℹ️  未直接匹配到 View Key 按钮，保持面板打开并持续轮询提取...")

    # ── 第三阶段：持续轮询提取 key（最多 50 次 * 0.5s = 25 秒）───
    for attempt in range(50):
        await asyncio.sleep(0.5)

        # 扫描所有框架提取 key
        val = await _scan_all_frames_for_key()
        if val:
            _emit(cb, f"  ✅ 提取到 key: {val}")
            return val

        # 若此前未能点击 View Key，后续每隔 3 次尝试重新探测一次按钮（应对按钮延迟加载）
        if not view_key_clicked and attempt % 3 == 0:
            click_res = await _click_view_key_in_any_frame()
            if click_res:
                _emit(cb, f"  🔘 延迟发现并点击 View Key 按钮: '{click_res.get('text')}'")
                view_key_clicked = True
        elif view_key_clicked and attempt > 0 and attempt % 8 == 0:
            # 已点击但 key 迟迟未下发，再次尝试触发点击（防止首次点击事件偶发丢失）
            re_click = await _click_view_key_in_any_frame()
            if re_click:
                _emit(cb, f"  🔘 再次触发 View Key 点击响应...")

    # 失败截图
    ss_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screenshots")
    os.makedirs(ss_dir, exist_ok=True)
    slug = product_name.replace(" ", "_").replace("/", "_")[:30]
    try:
        await page.screenshot(path=os.path.join(ss_dir, f"key_fail_{slug}.png"))
    except Exception:
        pass
    _emit(cb, f"  ❌ 点击 View Key 后仍未提取到 key")
    return ""


async def _close_panel(page: Page):
    """关闭右侧详情面板（仅使用 Escape 键，严禁点击任何可能属于主 Education Blade 的关闭按钮）。"""
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _clear_search(page: Page):
    """彻底清空内部搜索框内容（点击清空按钮 + 原生全选删除 + React input 事件派发）。"""
    for frame in page.frames:
        try:
            # 1. 尝试点击清空按钮 (Clear icon)
            for clear_btn in [
                "button[aria-label*='clear' i]",
                "button[aria-label*='清空' i]",
                ".ms-SearchBox-clearButton",
                "[class*='clearButton']",
                "[data-icon-name='Clear']",
                "[data-icon-name='Cancel']"
            ]:
                try:
                    loc = frame.locator(clear_btn).first
                    if await loc.is_visible(timeout=100):
                        await loc.click()
                        await asyncio.sleep(0.1)
                except Exception:
                    pass

            # 2. JS 强制清空所有搜索框 value 并派发事件
            await frame.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll('input')].filter(i => {
                        const r = i.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const ph = (i.placeholder || '').toLowerCase();
                        const al = (i.getAttribute('aria-label') || '').toLowerCase();
                        if (ph.includes('search resources') || al.includes('search resources')) return false;
                        return ph.includes('search') || al.includes('search') || i.type === 'search';
                    });
                    for (const input of inputs) {
                        input.focus();
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        if (setter) setter.call(input, '');
                        else input.value = '';
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        input.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }
            """)
        except Exception:
            pass

    # 3. 键盘全选删除兜底
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    await asyncio.sleep(0.2)


async def _handle_portal_login(page: Page, totp_secret: str,
                                ms_email: str, cb: ProgressCallback) -> str:
    """
    处理 portal 新标签页的 2FA 验证 / 2FA 绑定注册全流程：
    统一调用 modules.azure_login._handle_mfa_setup 响应式状态机完成：
      - 「Let's keep your account secure」点 Next
      - 「Install Microsoft Authenticator」点「Set up a different authentication app」
      - 「Scan the QR code」点「Can't scan the QR code?」提取 Secret
      - 「Enter the code」生成并填入 TOTP 验证码提交
      - 「Authenticator app added / Done」点完成
      - 「Stay signed in」点 No 并进入 Azure Portal
    """
    from modules.azure_login import _handle_mfa_setup, _azure_destination_ready

    # 如果已经在 portal 且脱离登录/2FA流程
    if await _azure_destination_ready(page, page.url or ""):
        return totp_secret

    return await _handle_mfa_setup(page, cb, existing_secret=totp_secret)


async def _click_first_visible(page: Page, selectors: list, timeout: int = 4000) -> bool:
    """按顺序尝试点击第一个可见的选择器，带超时保护与 force 点击。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=timeout, force=True)
                return True
        except Exception:
            continue
    return False


async def _click_next_robust(page: Page, cb: ProgressCallback = None) -> bool:
    """点击 Next / Verify / Submit 按钮（多种方式尝试，含 force + scroll）。"""
    for sel in [
        "button#idSubmit_SAOTCC_Continue",
        "input#idSubmit_SAOTCC_Continue",
        "input#idSubmit_ProofUp_Redirect",
        "input#idSIButton9",
        "button#idSIButton9",
        "button:has-text('Verify')",
        "button:has-text('验证')",
        "input[value='Verify']",
        "input[value='验证']",
        "button:has-text('Next')",
        "button:has-text('下一步')",
        "input[type='submit'][value='Next']",
        "input[type='submit'][value='下一步']",
        "input[type='submit'][value='Sign in']",
        "button[type='submit']",
        "input[type='submit']",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=3000, force=True)
                return True
        except Exception:
            continue
    try:
        clicked = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button, input[type=submit]')];
                const next = btns.find(b => {
                    const t = (b.innerText || b.value || '').trim().toLowerCase();
                    return t === 'next' || t === '下一步' || t === 'verify' || t === '验证' || t === 'sign in';
                });
                if (next && !next.disabled) { next.click(); return true; }
                return false;
            }
        """)
        return bool(clicked)
    except Exception:
        return False


async def _click_link_by_text(page: Page, texts: list) -> bool:
    """按文本点击链接/按钮。"""
    for text in texts:
        try:
            clicked = await page.evaluate("""
                (text) => {
                    const els = [...document.querySelectorAll('a, button, [role=link], [role=button]')];
                    const target = els.find(el => {
                        const t = (el.innerText || el.textContent || '').trim();
                        return t === text || t.includes(text);
                    });
                    if (target) { target.click(); return true; }
                    return false;
                }
            """, text)
            if clicked:
                return True
        except Exception:
            continue
    return False


async def _fill_totp_code(page: Page, code: str, cb: ProgressCallback) -> bool:
    """填入 TOTP 验证码到输入框。"""
    sels = [
        "input[name='otc']",
        "input#idTxtBx_SAOTCC_OTC",
        "input#idTxtBx_OTC",
        "input[name='VerificationCode']",
        "input#VerificationCode",
        "input[autocomplete='one-time-code']",
        "input#otc",
        "input[placeholder*='code' i]",
        "input[placeholder*='代码' i]",
        "input[placeholder*='验证码' i]",
        "input[aria-label*='code' i]",
        "input[aria-label*='代码' i]",
        "input[aria-label*='验证码' i]",
        "input[type='tel']",
    ]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                await loc.click()
                await loc.fill("")
                await loc.fill(code)
                actual = await loc.input_value()
                if actual.strip() == code:
                    _emit(cb, f"  ✅ TOTP 已填入")
                    return True
        except Exception:
            continue
    try:
        ok = await page.evaluate("""
            (code) => {
                const inputs = [...document.querySelectorAll('input')].filter(i => {
                    const r = i.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && !i.disabled && !i.readOnly
                        && i.type !== 'hidden' && i.type !== 'submit' && i.type !== 'button';
                });
                if (inputs.length === 0) return false;
                const target = inputs[0];
                target.focus();
                target.value = code;
                target.dispatchEvent(new Event('input', {bubbles:true}));
                target.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }
        """, code)
        if ok:
            _emit(cb, "  ✅ TOTP 已填入（JS）")
            return True
    except Exception:
        pass
    _emit(cb, "  ❌ TOTP 填入失败")
    return False


_TOTP_SECRET_BLACKLIST_WORDS = (
    "SKIP", "MAIN", "CONTENT", "AUTHENTICATOR", "AUTHENTICAT", "MICROSOFT",
    "ACCOUNT", "SETUP", "CANTSCAN", "QRCODE", "COPY", "ENTER",
    "CODE", "MANUALLY", "ENGLISH", "PRIVACY", "TERMS", "HELP",
    "ENABLE", "JAVASCRIPT", "RUNTHISAPP", "BROWSER", "SIGNIN",
    "PASSWORD", "SECURITY", "NOTIFICATION", "VERIFICATION",
    "IDENTITY", "CONDITIONS", "FEEDBACK", "LANGUAGE", "MORE",
    "OPTIONS", "CONTINUE", "CANCEL", "SUBMIT", "FINISH", "DONE",
    "BACK", "NEXT", "PROTECTION", "REGISTER", "PORTAL",
    "UNIVERSITY", "STUDENT", "EDUCATION", "AZURE", "COMMUNITY",
    "SUPPORT", "LEGAL", "CONTACT", "OFFICE", "WINDOWS", "DEFAULT",
    "BUTTON", "TITLE", "LABEL", "INPUT", "ACTION", "NOSCRIPT",
    "YOUNEEDTOENABLE", "FOLLOWING", "SCANNER", "SELECT", "ACCOUNTNAME",
    "SECRETKEY", "COPYNAME", "COPYKEY", "ENTERCODE", "OPENTHEQR"
)


def _is_valid_totp_secret(candidate: str | None) -> bool:
    """校验候选字符串是否为合法的 TOTP Base32 密钥。"""
    if not candidate:
        return False
    clean = re.sub(r"[\s-]+", "", candidate.upper())
    clean = re.sub(r"^SECRET\s*KEY\s*:?\s*", "", clean)
    clean = re.sub(r"(?:COPYKEY|COPY|COPYNAME)$", "", clean)
    if len(clean) < 16 or len(clean) > 64:
        return False
    if not re.fullmatch(r"[A-Z2-7]+", clean):
        return False
    if any(bw in clean for bw in _TOTP_SECRET_BLACKLIST_WORDS):
        return False
    try:
        padding = "=" * ((8 - len(clean) % 8) % 8)
        decoded = base64.b32decode(clean + padding, casefold=True)
        if len(decoded) < 10:
            return False
        pyotp.TOTP(clean).now()
        return True
    except Exception:
        return False


async def _extract_secret_key_robust(page: Page, cb: ProgressCallback) -> str:
    """从「Enter the following into Authenticator」页提取 Secret key (优先精确定位 Secret Key 标签/容器，严格排除任何长文本与黑名单单词)。"""
    for try_i in range(15):
        await asyncio.sleep(1)
        try:
            candidate = await page.evaluate(r"""
                () => {
                    const BLACKLIST = [
                        'SKIP', 'MAIN', 'CONTENT', 'AUTHENTICATOR', 'AUTHENTICAT', 'MICROSOFT',
                        'ACCOUNT', 'SETUP', 'CANTSCAN', 'QRCODE', 'COPY', 'ENTER',
                        'CODE', 'MANUALLY', 'ENGLISH', 'PRIVACY', 'TERMS', 'HELP',
                        'ENABLE', 'JAVASCRIPT', 'RUNTHISAPP', 'BROWSER', 'SIGNIN',
                        'PASSWORD', 'SECURITY', 'NOTIFICATION', 'VERIFICATION',
                        'IDENTITY', 'CONDITIONS', 'FEEDBACK', 'LANGUAGE', 'MORE',
                        'OPTIONS', 'CONTINUE', 'CANCEL', 'SUBMIT', 'FINISH', 'DONE',
                        'BACK', 'NEXT', 'PROTECTION', 'REGISTER', 'PORTAL',
                        'UNIVERSITY', 'STUDENT', 'EDUCATION', 'AZURE', 'COMMUNITY',
                        'SUPPORT', 'LEGAL', 'CONTACT', 'OFFICE', 'WINDOWS', 'DEFAULT',
                        'BUTTON', 'TITLE', 'LABEL', 'INPUT', 'ACTION', 'NOSCRIPT',
                        'YOUNEEDTOENABLE', 'FOLLOWING', 'SCANNER', 'SELECT', 'ACCOUNTNAME',
                        'SECRETKEY', 'COPYNAME', 'COPYKEY', 'ENTERCODE', 'OPENTHEQR'
                    ];
                    const clean = (s) => (s || '').trim().replace(/[\s-]+/g, '').toUpperCase();
                    const isBase32 = (s) => {
                        if (!s || s.length < 16 || s.length > 64) return false;
                        if (!/^[A-Z2-7]+$/.test(s)) return false;
                        for (const w of BLACKLIST) {
                            if (s.includes(w)) return false;
                        }
                        return true;
                    };

                    const allEls = [...document.querySelectorAll('*')];

                    // 策略1: 定位「Secret key:」文字节点，从其相邻兄弟元素或父容器中提取密钥值
                    for (const el of allEls) {
                        if (el.children.length > 3) continue;
                        const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (txt === 'secret key:' || txt === 'secret key' || txt.startsWith('secret key:')) {
                            // 1.1 检查后续兄弟节点
                            let sib = el.nextElementSibling;
                            while (sib) {
                                const sTxt = clean(sib.innerText || sib.textContent || sib.value || '');
                                if (isBase32(sTxt)) return sTxt;
                                const innerEls = sib.querySelectorAll('*');
                                for (const inner of innerEls) {
                                    if (inner.children.length === 0) {
                                        const cTxt = clean(inner.innerText || inner.textContent || '');
                                        if (isBase32(cTxt)) return cTxt;
                                    }
                                }
                                sib = sib.nextElementSibling;
                            }

                            // 1.2 检查父级容器中排除 "Secret key" 后的文本
                            let parent = el.parentElement;
                            for (let depth = 0; depth < 4 && parent; depth++) {
                                const pText = parent.innerText || parent.textContent || '';
                                const m = pText.match(/secret\s*key\s*[:：]?\s*([a-zA-Z2-7\s-]{16,64})/i);
                                if (m) {
                                    const candidate = clean(m[1].replace(/copy.*$/i, '').replace(/复制.*$/i, ''));
                                    if (isBase32(candidate)) return candidate;
                                }
                                for (const child of parent.querySelectorAll('*')) {
                                    if (child.children.length === 0) {
                                        const cTxt = clean(child.innerText || child.textContent || '');
                                        if (isBase32(cTxt)) return cTxt;
                                    }
                                }
                                parent = parent.parentElement;
                            }
                        }
                    }

                    // 策略2: 定位「Copy key」/「复制密钥」按钮，取其前序或关联容器元素
                    for (const el of allEls) {
                        const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
                        if (txt === 'copy key' || txt === '复制密钥' || txt.includes('copy key')) {
                            let prev = el.previousElementSibling;
                            while (prev) {
                                const pTxt = clean(prev.innerText || prev.textContent || prev.value || '');
                                if (isBase32(pTxt)) return pTxt;
                                prev = prev.previousElementSibling;
                            }
                            let parent = el.parentElement;
                            if (parent) {
                                for (const child of parent.querySelectorAll('*')) {
                                    if (child.children.length === 0) {
                                        const cTxt = clean(child.innerText || child.textContent || '');
                                        if (isBase32(cTxt)) return cTxt;
                                    }
                                }
                            }
                        }
                    }

                    // 策略3: 优先从专有属性与元素查找
                    const specificEls = document.querySelectorAll(
                        "[id*='secret' i], [data-bind*='secret' i], [data-testid*='secret' i], code, pre, .secret-key, input[readonly], [aria-label*='secret' i]"
                    );
                    for (const el of specificEls) {
                        if (el.closest('noscript, script, style, header, footer, nav')) continue;
                        const val = clean(el.value || el.innerText || el.textContent || el.getAttribute('data-value') || el.getAttribute('aria-label') || '');
                        if (isBase32(val)) return val;
                    }

                    // 策略4: 全文严格正则匹配 "Secret key: XXXXX"
                    const bodyText = (document.body ? (document.body.innerText || document.body.textContent || '') : '');
                    const m = bodyText.match(/Secret\s*key\s*[:：]?\s*([a-zA-Z2-7\s-]{16,64})/i);
                    if (m) {
                        const candidate = clean(m[1].replace(/copy.*$/i, '').replace(/复制.*$/i, ''));
                        if (isBase32(candidate)) return candidate;
                    }

                    // 策略5: 单独独立的叶子文本节点（内容完全就是 16~32 位的纯 Base32，绝不从句子中切词）
                    const walker = document.createTreeWalker(
                        document.body || document.documentElement,
                        NodeFilter.SHOW_TEXT,
                        {
                            acceptNode: (node) => {
                                const p = node.parentElement;
                                if (!p) return NodeFilter.FILTER_REJECT;
                                const tag = p.tagName.toLowerCase();
                                if (tag === 'script' || tag === 'noscript' || tag === 'style' || tag === 'header' || tag === 'footer' || tag === 'nav') {
                                    return NodeFilter.FILTER_REJECT;
                                }
                                const r = p.getBoundingClientRect();
                                if (r.width === 0 || r.height === 0) return NodeFilter.FILTER_REJECT;
                                const s = window.getComputedStyle(p);
                                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') {
                                    return NodeFilter.FILTER_REJECT;
                                }
                                return NodeFilter.FILTER_ACCEPT;
                            }
                        }
                    );
                    let node;
                    while (node = walker.nextNode()) {
                        const raw = (node.textContent || '').trim();
                        const c = clean(raw);
                        if (/^[A-Z2-7]{16,32}$/.test(c) && isBase32(c)) {
                            return c;
                        }
                    }

                    return '';
                }
            """)
            if candidate and _is_valid_totp_secret(candidate):
                return candidate.replace(" ", "").replace("-", "").upper()
        except Exception as e:
            if try_i == 0:
                _emit(cb, f"  ⚠️  JS 提取异常: {e}")

    return ""


async def extract_all_keys(
    page: Page,
    ctx: BrowserContext,
    totp_secret: str,
    ms_email: str,
    cb: ProgressCallback = None,
    proxy_str: str | None = None,
    proxy_ctrl: Optional[Any] = None,
) -> tuple[dict, str]:
    """
    提取所有目标产品的 key。
    复用当前 page 页面避免重复冷加载。
    """
    keys = {}
    _EDU_SW_URL = (
        "https://portal.azure.com/#view/Microsoft_Azure_Education"
        "/EducationMenuBlade/~/software"
    )

    if page and not page.is_closed():
        sw_page = page
    else:
        _emit(cb, "  🌐 新开标签页 → Azure Education Software...")
        sw_page = await ctx.new_page()

    sw_page.set_default_timeout(8000)

    # 如果不在 Education Software 页面，则导航
    if "EducationMenuBlade" not in (sw_page.url or ""):
        for attempt in range(3):
            try:
                await sw_page.goto(_EDU_SW_URL, wait_until="domcontentloaded",
                                   timeout=config.PAGE_LOAD_TIMEOUT)
                break
            except Exception as e:
                if attempt < 2:
                    _emit(cb, f"  ⚠️  导航失败（第{attempt+1}次）: {str(e)[:60]}，重试...")
                    await asyncio.sleep(3)
                else:
                    raise

    # 处理 portal 可能出现的 2FA / MFA 流程
    new_totp = await _handle_portal_login(sw_page, totp_secret, ms_email, cb)
    updated_totp = new_totp or totp_secret
    if new_totp:
        _emit(cb, f"  🔑 MFA Secret 已更新: {new_totp}")

    try:
        # 等待 portal SPA 加载完成，期间持续响应 2FA 验证
        _, updated_totp = await _wait_for_portal_ready(
            sw_page, cb, timeout=config.PORTAL_WAIT,
            totp_secret=updated_totp, ms_email=ms_email, proxy_ctrl=proxy_ctrl
        )
        await asyncio.sleep(0.5)

        # ── 处理 Terms Acceptance ────────────────────────────
        await _handle_terms_flow(sw_page, cb)

        # ── 等待软件列表加载（必须确认出现产品数据行）─────────────────
        _emit(cb, "  ⏳ 等待软件列表加载...")
        list_ready = False
        for w in range(40):
            await asyncio.sleep(1)
            for frame in [sw_page] + list(sw_page.frames):
                try:
                    has_data = await frame.evaluate("""
                        () => {
                            const items = [...document.querySelectorAll('a, [role="row"], [role="gridcell"]')];
                            return items.some(el => {
                                const t = (el.innerText || el.textContent || '').toLowerCase();
                                return t.includes('visual studio') || t.includes('windows') || t.includes('sql server') || t.includes('access') || t.includes('project') || t.includes('visio');
                            });
                        }
                    """)
                    if has_data:
                        list_ready = True
                        break
                except Exception:
                    pass
            if list_ready:
                _emit(cb, f"  ✅ 软件列表已就绪（{w+1}s）")
                break

            # 如果 10 秒后仍未就绪且出现红条横幅，自动重试一次条款接受
            if w == 10:
                try:
                    has_red = await sw_page.evaluate("() => (document.body.innerText || '').includes('accept the terms')")
                    if has_red:
                        _emit(cb, "  ⚠️ 检测到条款横幅依然存在，重新触发条款接受...")
                        await _handle_terms_flow(sw_page, cb)
                except Exception:
                    pass

        await asyncio.sleep(0.5)

        # ── 对每个产品提取 key ───────────────────────────────
        failed_products = []

        for product_name in config.PRODUCTS_TO_EXTRACT:
            # 检查是否偏离 Education Software 页面（例如意外退回 #home）
            cur_u = (sw_page.url or "").lower()
            if "educationmenublade" not in cur_u or "#home" in cur_u:
                _emit(cb, "  ⚠️ 检测到偏离 Education Software 页面，重新导航回到软件页...")
                try:
                    await sw_page.goto(config.AZURE_EDU_SOFTWARE_URL, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(2)
                except Exception:
                    pass

            _emit(cb, f"\n  📦 处理产品: {product_name}")
            search_term = _PRODUCT_SEARCH_MAP.get(product_name, product_name.split()[0])

            searched = await _search_product(sw_page, search_term, cb)
            if not searched:
                _emit(cb, f"  ⚠️  搜索失败，跳过: {product_name}")
                keys[product_name] = ""
                failed_products.append(product_name)
                continue

            await asyncio.sleep(0.5)

            # 点击产品行
            clicked = await _click_product_row(sw_page, product_name, cb)
            if not clicked:
                _emit(cb, f"  ⚠️  未找到产品行，跳过: {product_name}")
                keys[product_name] = ""
                failed_products.append(product_name)
                await _clear_search(sw_page)
                continue

            # 提取 key
            key = await _extract_key_from_panel(sw_page, product_name, cb, existing_keys=list(keys.values()))

            keys[product_name] = key

            if key:
                _emit(cb, f"  🎉 {product_name[:40]}: {key}")
            else:
                _emit(cb, f"  ❌ {product_name[:40]}: 未获取到 key")
                failed_products.append(product_name)

            # 关闭详情面板并清空搜索框准备下一个产品
            await _close_panel(sw_page)
            await _clear_search(sw_page)
            # 拟人化自然交互停顿，防止触发微软 RP 高频风控
            await asyncio.sleep(random.uniform(0.8, 1.8))

        # ── 二次重试冗余机制（若有失败产品且仍在软件页，自动重新尝试一次） ──
        if failed_products:
            _emit(
                cb,
                f"\n  🔄 [二次重试机制] 发现 {len(failed_products)} 个产品未获取成功，正在重新尝试: "
                + ", ".join(p[:20] for p in failed_products)
                + "..."
            )
            await _close_panel(sw_page)
            await _clear_search(sw_page)

            for product_name in list(failed_products):
                _emit(cb, f"\n  📦 [二次重试] 处理产品: {product_name}")
                search_term = _PRODUCT_SEARCH_MAP.get(product_name, product_name.split()[0])

                searched = await _search_product(sw_page, search_term, cb)
                if not searched:
                    _emit(cb, f"  ⚠️  [二次重试] 搜索失败，跳过: {product_name}")
                    await _close_panel(sw_page)
                    await _clear_search(sw_page)
                    continue

                await asyncio.sleep(0.5)

                clicked = await _click_product_row(sw_page, product_name, cb)
                if not clicked:
                    _emit(cb, f"  ⚠️  [二次重试] 未找到产品行，跳过: {product_name}")
                    await _close_panel(sw_page)
                    await _clear_search(sw_page)
                    continue

                key = await _extract_key_from_panel(sw_page, product_name, cb, existing_keys=list(keys.values()))

                if key:
                    keys[product_name] = key
                    _emit(cb, f"  🎉 [二次重试成功] {product_name[:40]}: {key}")
                else:
                    _emit(cb, f"  ❌ [二次重试仍然失败] {product_name[:40]}: 未获取到 key")

                await _close_panel(sw_page)
                await _clear_search(sw_page)

    except Exception as e:
        _emit(cb, f"  ❌ 提取 key 过程异常: {e}")
        ss_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        try:
            await sw_page.screenshot(path=os.path.join(ss_dir, "key_extract_error.png"))
        except Exception:
            pass

    return keys, updated_totp


async def _handle_terms_flow(sw_page: Page, cb: ProgressCallback):
    """
    处理 Terms 流程：
    1. Software 页检测红色横幅（最多等待 45 秒）
    2. 若 45 秒未检测到，自动刷新页面 (reload) 并重新检测 45 秒
    3. 若检测到横幅 → 点击「Please accept the terms here.」或进入 TermsAcceptanceBlade
    4. 进入 Terms Acceptance 页 → 勾选 checkbox（真实触发 React synthetic event） → 点「Accept and continue」
    5. 等待后端处理生效 → 返回 Software 页并验证横幅消失
    """
    has_banner = False
    detected_text = ""

    for check_round in range(1, 3):  # 最多 2 轮（第 1 轮 45s，未找到则刷新后第 2 轮 45s）
        if check_round == 1:
            _emit(cb, "  🔍 检测 Terms 协议横幅（每秒轮询，最多 45 秒）...")
        else:
            _emit(cb, "  🔄 45 秒未检测到横幅，正在刷新页面重新检测 Terms 横幅（最多 45 秒）...")
            try:
                await sw_page.reload(wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)
            except Exception as e:
                _emit(cb, f"  ⚠️ 刷新页面异常: {e}")

        for w in range(45):
            await asyncio.sleep(1)
            for frame in [sw_page] + list(sw_page.frames):
                try:
                    r = await frame.evaluate("""
                        () => {
                            const txt = (document.body ? document.body.innerText : '') || '';
                            if (txt.includes("couldn't confirm you have accepted") ||
                                txt.includes("could not confirm you have accepted") ||
                                txt.includes("accept the terms here") ||
                                txt.includes("Please accept the terms") ||
                                txt.includes("Terms Acceptance") ||
                                txt.includes("接受条款") ||
                                txt.includes("请在此处接受条款")) {
                                const m = txt.match(/[^\\n]*(couldn't confirm|could not confirm|accept the terms|Terms Acceptance|接受条款)[^\\n]*/i);
                                return { found: true, text: m ? m[0].substring(0, 200) : '' };
                            }
                            return { found: false };
                        }
                    """)
                    if isinstance(r, dict) and r.get("found"):
                        has_banner = True
                        detected_text = r.get("text", "")
                        break
                except Exception:
                    pass
            if has_banner:
                break

        if has_banner:
            break

    if not has_banner:
        _emit(cb, f"  ℹ️  无 Terms 横幅，直接进入软件提取")
        return

    _emit(cb, f"  📋 检测到 Terms 横幅/页面: {detected_text[:120]}")

    # 1. 检查是否已经在 Terms Acceptance 页面，若不是则点击链接
    on_terms = False
    for frame in [sw_page] + list(sw_page.frames):
        try:
            on_terms = await frame.evaluate("""
                () => {
                    const txt = (document.body ? document.body.innerText : '') || '';
                    return txt.includes('Terms Acceptance') ||
                           txt.includes('Please accept the terms and conditions') ||
                           txt.includes('Accept and continue');
                }
            """)
            if on_terms:
                break
        except Exception:
            pass

    if not on_terms:
        _emit(cb, "  🖱️  点击 Terms 链接...")
        clicked = False
        for frame in [sw_page] + list(sw_page.frames):
            for sel in [
                "text=/Please accept the terms here/i",
                "text=Please accept the terms here.",
                "text=/accept the terms here/i",
                "a:has-text('accept the terms')",
                "a:has-text('terms')",
                "button:has-text('accept the terms')",
            ]:
                try:
                    loc = frame.locator(sel).first
                    if await loc.is_visible(timeout=1000):
                        await loc.scroll_into_view_if_needed()
                        await loc.click(force=True)
                        clicked = True
                        _emit(cb, "  ✅ 已点击 Terms 链接")
                        break
                except Exception:
                    continue
            if clicked:
                break

        # 2. 等待 Terms Acceptance 页加载
        _emit(cb, "  ⏳ 等待 Terms Acceptance 页加载...")
        for w in range(20):
            await asyncio.sleep(1)
            for frame in [sw_page] + list(sw_page.frames):
                try:
                    on_terms = await frame.evaluate("""
                        () => {
                            const txt = (document.body ? document.body.innerText : '') || '';
                            return txt.includes('Terms Acceptance') ||
                                   txt.includes('Please accept the terms and conditions') ||
                                   txt.includes('Accept and continue');
                        }
                    """)
                    if on_terms:
                        _emit(cb, f"  ✅ Terms Acceptance 页已加载（{w+1}s）")
                        break
                except Exception:
                    pass
            if on_terms:
                break

    await asyncio.sleep(1)

    # 3. 勾选 checkbox（精准点击复选框方块与触发 React 状态）
    _emit(cb, "  ☑️  勾选协议复选框...")
    for attempt in range(5):
        # A. 优先直接点击方块图标或 input
        for frame in [sw_page] + list(sw_page.frames):
            for sel in [
                ".ms-Checkbox-checkbox",
                "input[type='checkbox']",
                "[role='checkbox']",
                ".ms-Checkbox",
            ]:
                try:
                    loc = frame.locator(sel).first
                    if await loc.is_visible(timeout=500):
                        await loc.scroll_into_view_if_needed()
                        await loc.click(force=True)
                        _emit(cb, f"  ✅ 点击复选框元素: {sel}")
                        break
                except Exception:
                    pass

        # B. JS 穿透触发 click() 驱动 React 合成事件
        for frame in [sw_page] + list(sw_page.frames):
            try:
                await frame.evaluate("""
                    () => {
                        const inputs = [...document.querySelectorAll('input[type=checkbox], [role=checkbox]')];
                        inputs.forEach(cb => {
                            if (!cb.checked && cb.getAttribute('aria-checked') !== 'true') {
                                cb.scrollIntoView({block: 'center'});
                                cb.click();
                                cb.dispatchEvent(new Event('input', {bubbles: true}));
                                cb.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                        });
                        const boxes = [...document.querySelectorAll('.ms-Checkbox-checkbox')];
                        boxes.forEach(b => {
                            b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                        });
                    }
                """)
            except Exception:
                pass

        await asyncio.sleep(0.5)

        # 检查是否已勾选
        is_checked = False
        for frame in [sw_page] + list(sw_page.frames):
            try:
                is_checked = await frame.evaluate("""
                    () => {
                        const cb = document.querySelector('input[type=checkbox], [role=checkbox]');
                        if (!cb) return false;
                        return cb.checked === true || cb.getAttribute('aria-checked') === 'true' || !!cb.closest('.is-checked');
                    }
                """)
                if is_checked:
                    _emit(cb, "  ✅ 协议复选框已确认处于勾选状态")
                    break
            except Exception:
                pass
        if is_checked:
            break

    await asyncio.sleep(0.8)

    # 4. 点 Accept and continue 提交
    _emit(cb, "  🔘 点击「Accept and continue」提交协议...")
    accept_clicked = False
    for attempt in range(10):
        for frame in [sw_page] + list(sw_page.frames):
            for sel in [
                "button:has-text('Accept and continue')",
                "button:has-text('Accept')",
                "button:has-text('接受并继续')",
                "button:has-text('接受')",
                "[role=button]:has-text('Accept and continue')",
                "[role=button]:has-text('Accept')",
            ]:
                try:
                    btn_loc = frame.locator(sel).first
                    if await btn_loc.is_visible(timeout=500):
                        await btn_loc.scroll_into_view_if_needed()
                        await btn_loc.click(force=True)
                        accept_clicked = True
                        _emit(cb, f"  ✅ 已点击提交按钮: {sel}")
                        break
                except Exception:
                    pass
            if accept_clicked:
                break

            # JS 兜底点击
            try:
                clicked_js = await frame.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button, [role=button], input[type=submit]')];
                        const target = btns.find(b => {
                            const t = (b.innerText || b.value || b.textContent || '').trim().toLowerCase();
                            return (t.includes('accept and continue') || t === 'accept' || t.includes('接受并继续') || t === '接受') && !t.includes('cancel');
                        });
                        if (target) {
                            target.removeAttribute('disabled');
                            target.removeAttribute('aria-disabled');
                            target.disabled = false;
                            target.scrollIntoView({block: 'center'});
                            target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                            target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                            target.click();
                            return true;
                        }
                        return false;
                    }
                """)
                if clicked_js:
                    accept_clicked = True
                    _emit(cb, "  ✅ 已点击提交按钮 (JS)")
                    break
            except Exception:
                pass

        if accept_clicked:
            break
        await asyncio.sleep(0.5)

    # 5. 等待后台协议确认完成（至少等待 5 秒让微软后端完成入库）
    _emit(cb, "  ⏳ 等待微软后台确认协议生效（5s）...")
    await asyncio.sleep(5)

    # 6. 重新导航回 Software 页面并校验横幅是否已消除
    _emit(cb, "  🔄 返回 Software 页...")
    try:
        await sw_page.goto(config.AZURE_EDU_SOFTWARE_URL,
                            wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    for _w in range(30):
        await asyncio.sleep(0.5)
        try:
            ready = await sw_page.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll('input')].filter(i => {
                        const r = i.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const ph = (i.placeholder || '').toLowerCase();
                        return ph.includes('search') || i.type === 'search';
                    });
                    return inputs.length > 0;
                }
            """)
            if ready:
                break
        except Exception:
            pass


