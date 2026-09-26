"""
Azure 登录模块
--------------
流程:
  1. 直接打开 https://signup.azure.com/studentverification?offerType=1
  2. 出现账号选择器 → 点「Use a different account」→ 输入 edu 邮箱 + 密码
  3. 处理「Let's keep your account secure」→ 点 Next
  4. 处理 MFA 注册（Install Microsoft Authenticator）
     → 点「Set up a different authentication app」
     → 点「Can't scan the QR code?」→ 提取 Secret key
     → 用 pyotp 生成 TOTP 码填入 → 点 Next → Done
  5. 填写 Your profile 表单（edu邮箱直接填入，无需验证码）
  6. 等待账户确认完成
  7. 新开标签页打开 portal.azure.com Education Software 页面提取 key
  8. 返回 totp_secret
"""


import asyncio
import base64
import logging
import os
import random
import re
import sys
import time
from typing import Any, Callable, Optional

import pyotp
from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from utils.captcha_detector import check_captcha_present
from utils.human_action import (
    human_bezier_move,
    human_settle,
    human_hover_and_click,
    human_type,
)

log = logging.getLogger(__name__)
ProgressCallback = Optional[Callable[[str], None]]


class LoginNetworkError(RuntimeError):
    """登录流程初始网络导航或页面加载失败。"""
    pass


class EmailInputTimeoutError(RuntimeError):
    """由于网络延迟或页面未加载导致邮箱输入框未出现或输入超时。"""
    pass


class AzureCaptchaError(RuntimeError):
    """触发 Azure / Arkose / SheerID 人机拼图验证码。"""
    pass


# ── 选择器 ────────────────────────────────────────────────────
SEL_EMAIL = "input[type='email'], input[name='loginfmt'], input#i0116"
SEL_PASS  = "input[type='password'], input#i0118, input[name='passwd']"
SEL_PASSWORD_SUBMIT = (
    "button[type='submit'], input[type='submit'], "
    "button#idSIButton9, input#idSIButton9, "
    "button:has-text('Sign in'), input[value='Sign in'], "
    "button:has-text('登录'), input[value='登录']"
)

_LOGIN_URL_KEYS = ("login.microsoftonline", "login.live.com", "microsoftonline.com")
_PORTAL_URL_KEYS = ("portal.azure.com", "signup.azure.com", "azure.microsoft.com")


_PERMANENT_LOGIN_MARKS = (
    "password is incorrect", "account or password is incorrect",
    "incorrect password", "doesn't exist", "does not exist",
    "account has been locked", "account is locked", "account is disabled",
    "account has been blocked", "username may be incorrect",
    "密码不正确", "密码错误", "帐户或密码不正确", "账户或密码不正确",
    "帐户不存在", "账户不存在", "已锁定", "已禁用", "已阻止",
)

_RISK_LOGIN_MARKS = (
    "password sign-in isn't available", "password sign-in is not available",
    "try another method", "too many times with an incorrect account or password",
    "too many sign-in attempts", "too many unsuccessful sign-in attempts",
    "temporarily locked", "temporarily blocked",
    "密码登录不可用", "请尝试其他方法", "尝试登录次数过多",
)

_LOGIN_ERROR_MARKS = _PERMANENT_LOGIN_MARKS + _RISK_LOGIN_MARKS + (
    "we couldn't sign you in", "we could not sign you in",
    "something went wrong", "sign-in failed", "signin failed",
    "登录失败", "无法登录", "出现问题",
)


async def _get_ms_login_error(page: Page) -> str:
    """读取微软登录页 DOM 错误标识，防止停留在错误提示中无谓死循环。"""
    selectors = (
        "#usernameError",
        "#passwordError",
        "#i0118Error",
        "[role='alert']",
        "[aria-live='assertive']",
        "[data-testid*='error' i]",
        "[class*='error' i]",
    )
    for selector in selectors:
        try:
            locators = page.locator(selector)
            count = min(await locators.count(), 5)
            for index in range(count):
                locator = locators.nth(index)
                if not await locator.is_visible(timeout=120):
                    continue
                text = " ".join((await locator.inner_text(timeout=300) or "").split())
                if text and any(mark in text.lower() for mark in _LOGIN_ERROR_MARKS):
                    return text[:500]
        except Exception:
            continue

    try:
        body_text = await page.locator("body").inner_text(timeout=700)
        body_lines = [" ".join(line.split()) for line in body_text.splitlines() if line.strip()]
        for line in body_lines:
            if any(mark in line.lower() for mark in _LOGIN_ERROR_MARKS):
                return line[:500]
    except Exception:
        pass
    return ""


def _emit(cb: ProgressCallback, msg: str):
    log.info(msg)
    if cb:
        cb(msg)


async def _click_first_visible(page: Page, selectors: list, timeout: int = 4000) -> bool:
    """组合多个选择器并发等待，只要列表里任意一个选择器可见，即刻使用真实平滑鼠标轨迹与真人悬停点击。"""
    # 1. 优先单个快速探测 (100ms 快速响应)
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=100):
                ok = await human_hover_and_click(page, loc, timeout=1500)
                if ok:
                    return True
        except Exception:
            pass

    # 2. 组合选择器并发等待
    try:
        combined = ", ".join(selectors)
        loc = page.locator(combined).first
        await loc.wait_for(state="visible", timeout=min(timeout, 2000))
        ok = await human_hover_and_click(page, loc, timeout=1500)
        if ok:
            return True
    except Exception:
        pass

    return False

    # 3. 原生 JS 极速 DOM 点击兜底（毫秒级执行，彻底杜绝 Playwright 动作就绪检测卡死）
    try:
        clicked = await page.evaluate("""
            (sels) => {
                for (const s of sels) {
                    try {
                        const el = document.querySelector(s);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && !el.disabled) {
                                el.focus();
                                el.click();
                                return true;
                            }
                        }
                    } catch(e) {}
                }
                const btns = [...document.querySelectorAll('button, input[type=submit], a')];
                for (const b of btns) {
                    const t = (b.innerText || b.value || '').trim().toLowerCase();
                    if (t === 'next' || t === '下一步' || t === 'verify' || t === '验证' || t === 'continue' || t === '继续') {
                        const r = b.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && !b.disabled) {
                            b.focus();
                            b.click();
                            return true;
                        }
                    }
                }
                return false;
            }
        """, selectors)
        return bool(clicked)
    except Exception:
        pass

    return False



async def _first_visible_locator(page: Page, selector: str):
    """返回组合选择器中真正可见且准备就绪的第一个元素，严格过滤移出屏幕的过度动画元素。"""
    try:
        locators = page.locator(selector)
        count = min(await locators.count(), 8)
        for index in range(count):
            locator = locators.nth(index)
            if await locator.is_visible(timeout=150):
                info = await locator.evaluate("""
                    (el) => {
                        const r = el.getBoundingClientRect();
                        const cls = (el.className || '').toString();
                        const style = window.getComputedStyle(el);
                        return {
                            w: r.width,
                            h: r.height,
                            offscreen: cls.includes('moveOffScreen'),
                            disabled: el.disabled || style.pointerEvents === 'none' || style.visibility === 'hidden'
                        };
                    }
                """)
                if isinstance(info, dict) and info.get("w", 0) > 0 and info.get("h", 0) > 0:
                    if not info.get("offscreen") and not info.get("disabled"):
                        return locator
    except Exception:
        pass
    return None


async def _fill_totp_code(page: Page, code: str, cb: ProgressCallback = None) -> bool:
    """填入 TOTP 验证码到输入框（支持多选择器、跨 iframe、JS 兜底与快速超时保护）。"""
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
    frames_to_try = [page] + list(page.frames)

    # 策略 1: 定位器精准查找与拟人/直接写入
    for f in frames_to_try:
        for sel in sels:
            try:
                loc = f.locator(sel).first
                if await loc.is_visible(timeout=250):
                    # 优先拟人化按键
                    ok = await human_type(f, loc, code, min_delay=0.04, max_delay=0.09)
                    if not ok:
                        # 兜底：直接 click 与 fill
                        try:
                            await loc.click(timeout=1000, force=True)
                            await loc.fill(code, timeout=1000)
                        except Exception:
                            pass
                    val = ""
                    try:
                        val = await loc.input_value(timeout=500)
                    except Exception:
                        pass
                    if val and code in val:
                        _emit(cb, "  ✅ TOTP 已填入")
                        await asyncio.sleep(random.uniform(0.2, 0.4))
                        try:
                            await loc.press("Enter", timeout=1000)
                        except Exception:
                            pass
                        return True
            except Exception:
                continue

    # 策略 2: JS 跨 Frame 深度穿透填入
    for f in frames_to_try:
        try:
            ok = await f.evaluate("""
                (code) => {
                    const otcSels = [
                        "input[name='otc']", "input#idTxtBx_SAOTCC_OTC", "input#idTxtBx_OTC",
                        "input[name='VerificationCode']", "input#VerificationCode",
                        "input[autocomplete='one-time-code']", "input#otc",
                        "input[placeholder*='code' i]", "input[placeholder*='代码' i]",
                        "input[placeholder*='验证码' i]", "input[aria-label*='code' i]",
                        "input[aria-label*='代码' i]", "input[aria-label*='验证码' i]",
                        "input[type='tel']"
                    ];
                    let target = null;
                    for (const s of otcSels) {
                        const el = document.querySelector(s);
                        if (el && !el.disabled && !el.readOnly) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) { target = el; break; }
                        }
                    }
                    if (!target) {
                        const txt = (document.body ? document.body.innerText : '').toLowerCase();
                        if ((txt.includes('enter code') || txt.includes('enter the code') ||
                             txt.includes('输入代码') || txt.includes('验证码')) &&
                            !txt.includes('scan the qr') && !txt.includes('start by getting the app')) {
                            const inputs = [...document.querySelectorAll('input')].filter(i => {
                                const r = i.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 && !i.disabled && !i.readOnly &&
                                       !['hidden', 'submit', 'button', 'checkbox', 'radio', 'password'].includes(i.type);
                            });
                            for (const inp of inputs) {
                                const ml = inp.getAttribute('maxlength');
                                if (!ml || parseInt(ml) <= 10) { target = inp; break; }
                            }
                        }
                    }
                    if (target) {
                        target.focus();
                        target.value = code;
                        target.dispatchEvent(new Event('input', {bubbles: true}));
                        target.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    return false;
                }
            """, code)
            if ok:
                _emit(cb, "  ✅ TOTP 已填入 (JS)")
                return True
        except Exception:
            continue

    _emit(cb, "  ❌ TOTP 填入失败")
    return False


async def _submit_visible_password(
    page: Page,
    password: str,
    submitted_forms: set[str],
    cb: ProgressCallback,
) -> bool:
    """填写当前可见密码框并精确确认提交；同一个页面/表单只在成功提交后记录并返回 True。"""
    last_dom_error: Exception | None = None
    password_page_seen = False

    for attempt in range(4):
        password_locator = await _first_visible_locator(page, SEL_PASS)
        if password_locator is None:
            if not password_page_seen:
                return False
            await asyncio.sleep(0.15)
            continue

        password_page_seen = True
        form_url = page.url
        try:
            from urllib.parse import urlparse
            netloc = urlparse(form_url).netloc.lower()
            input_id = await password_locator.get_attribute("id") or ""
            input_name = await password_locator.get_attribute("name") or ""
            signature = f"{form_url}|{input_id}|{input_name}"

            # 只要在同一域名（如 login.microsoftonline.com）已经提交过密码，绝对不发起二次提交
            if signature in submitted_forms or (netloc and netloc in submitted_forms):
                return False

            if attempt == 0:
                _emit(cb, "  🔒 检测到密码页，等待安全遥测就绪并输入密码...")
                await human_settle(page, min_s=1.2, max_s=2.2, target_locator=password_locator)

            # 1. 拟人化聚焦、清空并输入密码
            await human_type(page, password_locator, password, min_delay=0.04, max_delay=0.10)

            # 校验 DOM 中密码值是否真正填写成功
            val = await password_locator.input_value(timeout=1000)
            if not val:
                await password_locator.fill(password, timeout=10000)
                val = await password_locator.input_value(timeout=1000)

            if not val:
                _emit(cb, "  ⚠️  密码输入框尚未准备就绪（写入值为空），重试中...")
                await asyncio.sleep(0.3)
                continue

            # 输入后人类视线停顿 (0.4~0.8s)
            await asyncio.sleep(random.uniform(0.40, 0.80))

            # 2. 提交密码：优先拟人化移动并点击 Sign in 按钮提交
            submit_button = await _first_visible_locator(page, SEL_PASSWORD_SUBMIT)
            _emit(cb, "  ✅ 密码已填入，提交登录...")

            if submit_button is not None:
                clicked = await human_hover_and_click(page, submit_button, timeout=5000)
                if not clicked:
                    try:
                        await password_locator.press("Enter", timeout=3000, no_wait_after=True)
                    except Exception:
                        pass
            else:
                try:
                    await password_locator.press("Enter", timeout=5000, no_wait_after=True)
                except Exception:
                    pass

            # 记录已提交签名及域名，锁定防止重复提交打断请求
            submitted_forms.add(signature)
            if netloc:
                submitted_forms.add(netloc)

            # 3. 轮询验证页面响应（等待微软服务器处理并跳转，最多等 12 秒）
            submitted_successfully = False
            for _w in range(30):
                await asyncio.sleep(0.4)
                if page.url != form_url:
                    submitted_successfully = True
                    break
                cur_pass = await _first_visible_locator(page, SEL_PASS)
                if cur_pass is None:
                    submitted_successfully = True
                    break

            if submitted_successfully:
                _emit(cb, "  ✅ 密码表单已提交，页面响应成功")
            else:
                _emit(cb, "  ✅ 密码表单已提交，等待后续跳转...")
            return True

        except Exception as exc:
            message = str(exc).lower()
            transient_dom_error = any(
                marker in message
                for marker in (
                    "not attached to the dom",
                    "element is not attached",
                    "element was detached",
                    "element is not stable",
                )
            )
            if not transient_dom_error:
                raise

            if page.url != form_url:
                submitted_forms.add(signature)
                _emit(cb, "  ✅ 密码表单已提交，页面正在跳转")
                return True

            last_dom_error = exc
            await asyncio.sleep(0.35)

    if last_dom_error is not None:
        raise RuntimeError("登录表单持续刷新，密码输入元素无法稳定定位") from last_dom_error
    return False


async def _azure_destination_ready(page: Page, url: str) -> bool:
    """确认真正到达 Azure 业务页面，而不是 OAuth 流程中的短暂中转。"""
    url_lower = (url or "").lower()

    # 1. 还在微软登录中转流程中（OAuth / 2FA / KMSI / 密码页 / mysignins 等）
    if any(k in url_lower for k in ("login.microsoftonline", "login.live.com", "login.windows.net", "mysignins.microsoft.com", "account.activedirectory")):
        return False

    # 2. Portal 主控制台（必须确保已渲染主体，而不是即将重定向到 2FA 的空白骨架）
    if "portal.azure.com" in url_lower:
        try:
            portal_ready = await page.evaluate("""
                () => {
                    const u = window.location.href.toLowerCase();
                    if (u.includes('login.microsoftonline') || u.includes('login.live') || u.includes('mysignins')) return false;
                    return !!(
                        document.querySelector('.fxs-topbar') ||
                        document.querySelector('[class*="topbar"]') ||
                        document.querySelector('[placeholder*="Search"]') ||
                        document.querySelector('[aria-label*="Search"]') ||
                        document.querySelector('input[type="search"]') ||
                        (document.body && (document.body.innerText.includes('Education') ||
                                           document.body.innerText.includes('Software') ||
                                           document.body.innerText.includes('Azure')))
                    );
                }
            """)
            if portal_ready:
                return True
        except Exception:
            pass
        return False

    # 3. Azure Education 平台
    if "azureforeducation" in url_lower or "education.azure.com" in url_lower:
        return True

    # 4. Signup / 学生认证业务页面
    if "signup.azure.com" in url_lower:
        # 排除发起登录的授权重定向中转
        if "/api/user/login" in url_lower:
            return False
        # 只要已经离开授权接口，到达具体业务路径（如 studentverification 等），代表 2FA 与登录已圆满完成！
        return True

    # 5. Azure 官网学生入口
    if "azure.microsoft.com" in url_lower:
        return True

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


async def _extract_secret_key_from_page(page: Page) -> str:
    """从页面深度提取 Base32 Secret Key (优先精确定位 Secret Key 标签/容器，严格排除任何长文本与黑名单单词)。"""
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
    except Exception:
        pass
    return ""


async def _handle_mfa_setup(page: Page, cb: ProgressCallback, existing_secret: str = "") -> str:
    """
    处理 MFA 注册流程：采用多状态响应式状态机，实时根据当前屏幕 DOM 状态执行对应操作：
      - 'keep_secure' -> 点 Next / 下一步
      - 'install_auth' -> 点「Set up a different authentication app」/「Other options」
      - 'setup_account' -> 确认切换选项并点击 Next (持续推进直到页面跳转)
      - 'scan_qr'     -> 点「Can't scan image? / Can't scan the QR code?」
      - 'show_secret' -> 深度遍历提取 Secret key 并点击 Next
      - 'enter_code'  -> 实时生成 TOTP 验证码填入并提交 Verify
      - 'done'        -> 点 Done / Finish / Continue
      - 'kmsi'        -> 点 No / Cancel
    """
    _emit(cb, "  🔐 开始 MFA 注册流程 (响应式状态机)...")
    secret = existing_secret or ""
    last_state = ""
    same_state_count = 0
    consecutive_totp_fails = 0

    for step in range(80):  # 最多轮询 ~40 秒
        await asyncio.sleep(0.5)
        cur_url = (page.url or "").lower()

        # 检查是否已脱离 MFA 注册流程，进入目标页面
        if await _azure_destination_ready(page, cur_url):
            _emit(cb, "  ✅ 已进入 Azure 业务页面，MFA 注册完成")
            return secret

        # 读取页面标题与 DOM 文本 (包含所有 headings, labels, buttons, 角色块与主体文本)
        try:
            info = await page.evaluate("""
                () => {
                    const title = document.title || '';
                    const headings = [...document.querySelectorAll('h1,h2,h3,label,p,a,button,div[role="heading"],.text-title,#displayName,#loginHeader')]
                        .map(el => el.innerText.trim())
                        .filter(t => t.length > 0 && t.length < 200);
                    const bodyText = (document.body ? document.body.innerText : '').slice(0, 1500);
                    return { title, headings: headings.join(' | ') + ' | ' + bodyText };
                }
            """)
            t = (info.get("title", "") + " " + info.get("headings", "")).lower()
        except Exception:
            t = ""

        # 判定当前界面状态
        has_visible_secret = False
        try:
            has_visible_secret = await page.evaluate(r"""
                () => {
                    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
                    let node;
                    while (node = walker.nextNode()) {
                        const el = node.parentElement;
                        if (!el) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const t = (node.textContent || '').trim().toLowerCase();
                        if (t.includes('secret key') || t.includes('enter the following into authenticator')) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
        except Exception:
            pass

        has_code_input = False
        try:
            has_code_input = await page.evaluate("""
                () => {
                    const sels = [
                        "input[name='otc']", "input#idTxtBx_SAOTCC_OTC", "input#idTxtBx_OTC",
                        "input[name='VerificationCode']", "input#VerificationCode",
                        "input[autocomplete='one-time-code']", "input#otc",
                        "input[placeholder*='code' i]", "input[placeholder*='代码' i]",
                        "input[placeholder*='验证码' i]", "input[aria-label*='code' i]",
                        "input[aria-label*='代码' i]", "input[aria-label*='验证码' i]"
                    ];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && !el.disabled && !el.readOnly) return true;
                        }
                    }
                    // 仅当页面明确包含 enter code / verification 等提示且无 scan / setup app 时，查找短文本框
                    const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
                    const isCodePrompt = (bodyText.includes('enter code') || bodyText.includes('enter the code') ||
                                         bodyText.includes('输入代码') || bodyText.includes('验证码')) &&
                                         !bodyText.includes('scan the qr') && !bodyText.includes('start by getting the app');
                    if (isCodePrompt) {
                        const inputs = [...document.querySelectorAll('input')].filter(i => {
                            const r = i.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && !i.disabled && !i.readOnly &&
                                   !['hidden', 'submit', 'button', 'checkbox', 'radio', 'password'].includes(i.type);
                        });
                        for (const inp of inputs) {
                            const ml = inp.getAttribute('maxlength');
                            if (!ml || parseInt(ml) <= 10) return true;
                        }
                    }
                    return false;
                }
            """)
        except Exception:
            pass

        state = "unknown"
        # 1. 优先检查 KMSI (Stay signed in)
        if "stay signed in" in t or "保持登录" in t or "kmsi" in cur_url:
            state = "kmsi"
        # 2. 成功通过页（Notification approved / Great job / Authenticator app added / Success / App registered）
        elif any(k in t for k in (
            "notification approved", "great job", "successfully registered", "registered",
            "authenticator app added", "authenticator app was successfully",
            "you're all set", "success", "已成功注册", "成功", "完成"
        )) and ("enter the following" not in t and "scan" not in t and "enter code" not in t and "enter the code" not in t):
            state = "done"
        # 3. 保持账号安全页（Let's keep your account secure / More information required）
        elif ("let's keep your account secure" in t or "keep your account secure" in t or
              "more information required" in t or "需要详细信息" in t or "保护帐户安全" in t or "保护账户安全" in t):
            state = "keep_secure"
        # 4. 安装验证器页（Start by getting the app / Install Microsoft Authenticator）
        elif "install microsoft authenticator" in t or "start by getting the app" in t or "获取应用" in t:
            state = "install_auth"
        # 5. 配置账号页（Set up your account in app）
        elif ("set up your account in app" in t or "set up your account" in t or "在应用中设置" in t) and "enter the code" not in t and "enter code" not in t:
            state = "setup_account"
        # 6. 显示 Secret Key 页
        elif has_visible_secret or ("enter the following" in t and "scan" not in t) or ("secret key" in t and "scan" not in t):
            state = "show_secret"
        # 7. 扫描二维码页
        elif "scan the qr code" in t or "scan image" in t or "can't scan" in t or "扫描二维码" in t:
            state = "scan_qr"
        # 8. 填入 TOTP 码页 (Enter the code / 2FA 动态码校验)
        elif has_code_input or (any(k in t for k in ("enter the code", "enter code", "verification code", "输入代码", "验证码")) and "scan" not in t and "start by getting the app" not in t):
            state = "enter_code"

        # 9. 状态未识别但包含 Next/下一步 按钮时，作为 keep_secure 推进按钮兜底
        if state == "unknown":
            try:
                has_next_btn = await page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button, input[type=submit], input[type=button]')];
                        return btns.some(b => {
                            const r = b.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0 || b.disabled) return false;
                            const t = (b.innerText || b.value || '').trim().toLowerCase();
                            return t === 'next' || t === '下一步' || t === 'verify' || t === '验证' || t === 'continue' || t === '继续';
                        });
                    }
                """)
                if has_next_btn and any(k in cur_url for k in ("mysignins.microsoft.com", "login.microsoftonline", "login.live.com", "account.activedirectory")):
                    state = "keep_secure"
            except Exception:
                pass

        if state == last_state:
            same_state_count += 1
        else:
            same_state_count = 0
            last_state = state

        # ── 执行对应状态操作 ──────────────────────────────
        if state == "keep_secure":
            _emit(cb, "  🔐 「Let's keep your account secure」→ 点 Next")
            await _click_first_visible(page, [
                "input#idSubmit_ProofUp_Redirect",
                "input#idSIButton9",
                "button#idSIButton9",
                "button:has-text('Next')", "input[value='Next']",
                "button:has-text('下一步')", "input[value='下一步']",
                "input[type='submit']", "button[type='submit']"
            ], timeout=2500)
            await asyncio.sleep(0.5)
            continue

        if state == "install_auth":
            _emit(cb, "  📱 「Install Microsoft Authenticator」→ 切换第三方验证器...")
            clicked_alt = False
            for sel in [
                "a:has-text('I want to use a different authenticator app')",
                "button:has-text('I want to use a different authenticator app')",
                "a:has-text('Set up a different authentication app')",
                "button:has-text('Set up a different authentication app')",
                "a:has-text('I want to set up a different method')",
                "a:has-text('Other options')",
                "text='I want to use a different authenticator app'",
                "text='Set up a different authentication app'",
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=100):
                        await loc.scroll_into_view_if_needed()
                        await loc.click(force=True)
                        clicked_alt = True
                        break
                except Exception:
                    pass
            if not clicked_alt and same_state_count > 2:
                await _click_first_visible(page, ["button:has-text('Next')", "input[value='Next']"], timeout=2000)
            continue

        if state == "setup_account":
            _emit(cb, "  📲 「Set up your account in app」→ 点 Next 推进...")
            # 优先检查是否还有切换其他验证器的选项
            for alt_sel in [
                "a:has-text('I want to use a different authenticator app')",
                "button:has-text('I want to use a different authenticator app')",
                "a:has-text('Set up a different authentication app')",
                "a:has-text('Other options')",
                "text='I want to use a different authenticator app'",
            ]:
                try:
                    loc = page.locator(alt_sel).first
                    if await loc.is_visible(timeout=80):
                        await loc.click(force=True)
                        await asyncio.sleep(0.2)
                        break
                except Exception:
                    pass

            # 点击 Next 推进到下一步（二维码页）
            await _click_first_visible(page, [
                "button#idSubmit_SAOTCC_Continue",
                "input#idSubmit_SAOTCC_Continue",
                "button:has-text('Next')",
                "input[value='Next']",
                "button:has-text('下一步')",
                "input[value='下一步']"
            ], timeout=1500)
            try:
                await page.evaluate("""() => {
                    const btn = document.querySelector('#idSubmit_SAOTCC_Continue') ||
                                [...document.querySelectorAll('button, input[type=submit]')].find(b => (b.innerText || b.value || '').toLowerCase().includes('next'));
                    if (btn) btn.click();
                }""")
            except Exception:
                pass
            continue

        if state == "scan_qr":
            _emit(cb, "  📷 「Scan the QR code」→ 点「Can't scan the QR code?」...")
            for sel in [
                "a:has-text('Can\\'t scan image?')",
                "a:has-text('Can\\'t scan image')",
                "button:has-text('Can\\'t scan image')",
                "a:has-text('Can\\'t scan the QR code?')",
                "a:has-text('Can\\'t scan the QR code')",
                "button:has-text('Can\\'t scan the QR code')",
                "a:has-text('Can\\'t scan')",
                "button:has-text('Can\\'t scan')",
                "a:has-text('Enter code manually')",
                "text='Can\\'t scan the QR code?'",
                "text='无法扫描二维码'",
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=150):
                        await loc.scroll_into_view_if_needed()
                        await loc.click(timeout=1500, force=True)
                        _emit(cb, f"  ✅ 已点击: {sel}")
                        break
                except Exception:
                    pass

            # 点击「Can't scan」后，等待并直接提取 Secret Key（最多等待 3 秒）
            _emit(cb, "  ⏳ 等待 Secret key 渲染并提取...")
            for _w in range(15):
                await asyncio.sleep(0.2)
                cand = await _extract_secret_key_from_page(page)
                if cand and _is_valid_totp_secret(cand):
                    secret = cand.replace(" ", "").upper()
                    _emit(cb, f"  ✅ 提取到 MFA Secret: {secret}")
                    break

            # 成功提取 Secret 后，点击 Next 推进到 Enter the code 页
            if secret:
                await asyncio.sleep(0.2)
                await _click_first_visible(page, [
                    "button#idSubmit_SAOTCC_Continue",
                    "input#idSubmit_SAOTCC_Continue",
                    "button#idSIButton9",
                    "button:has-text('Next')",
                    "input[value='Next']",
                    "button:has-text('下一步')",
                    "input[value='下一步']"
                ], timeout=2000)
            continue

        if state == "show_secret":
            _emit(cb, "  🔑 提取 Secret key...")
            for _w in range(12):
                cand = await _extract_secret_key_from_page(page)
                if cand and _is_valid_totp_secret(cand):
                    new_sec = cand.replace(" ", "").upper()
                    if new_sec != secret:
                        secret = new_sec
                        _emit(cb, f"  ✅ 提取并更新 MFA Secret: {secret}")
                    else:
                        _emit(cb, f"  ✅ 提取到 MFA Secret: {secret}")
                    break
                await asyncio.sleep(0.2)

            if secret:
                await asyncio.sleep(0.2)
                await _click_first_visible(page, [
                    "button#idSubmit_SAOTCC_Continue",
                    "input#idSubmit_SAOTCC_Continue",
                    "button#idSIButton9",
                    "button:has-text('Next')",
                    "input[value='Next']",
                    "button:has-text('下一步')",
                    "input[value='下一步']"
                ], timeout=2000)
            else:
                _emit(cb, "  ⏳ Secret 尚未完全渲染，等待...")
            continue

        if state == "enter_code":
            if not secret:
                _emit(cb, "  ⚠️ 尚未提取到 secret，尝试从当前页面或后退找回...")
                cand = await _extract_secret_key_from_page(page)
                if cand and _is_valid_totp_secret(cand):
                    secret = cand.replace(" ", "").upper()
                    _emit(cb, f"  ✅ 找回 MFA Secret: {secret}")

                # 如果依然未获取到 secret，点击页面上的「←」后退按钮或「Back」返回提取 Secret
                if not secret:
                    for back_sel in [
                        "button[aria-label*='back' i]",
                        "button[aria-label*='后退' i]",
                        "button[aria-label*='返回' i]",
                        "button:has-text('←')",
                        "button:has-text('Back')",
                        "a:has-text('Back')",
                        "a:has-text('Other options')",
                        ".back-button",
                    ]:
                        try:
                            b_loc = page.locator(back_sel).first
                            if await b_loc.is_visible(timeout=100):
                                await b_loc.click(timeout=1500, force=True)
                                _emit(cb, f"  🔙 点击后退按钮找回 Secret: {back_sel}")
                                await asyncio.sleep(0.8)
                                break
                        except Exception:
                            pass
                    continue

            if secret:
                totp_code = pyotp.TOTP(secret).now()
                _emit(cb, f"  🔢 填入 TOTP 码: {totp_code}...")
                filled = await _fill_totp_code(page, totp_code, cb)
                if filled:
                    consecutive_totp_fails = 0
                    await asyncio.sleep(0.3)
                    await _click_first_visible(page, [
                        "button#idSubmit_SAOTCC_Continue", "input#idSubmit_SAOTCC_Continue",
                        "button#idSIButton9", "input#idSIButton9",
                        "button:has-text('Next')", "input[value='Next']",
                        "button:has-text('Verify')", "input[value='Verify']",
                        "button:has-text('下一步')", "button:has-text('验证')"
                    ], timeout=2500)
                    for _w in range(16):
                        await asyncio.sleep(0.25)
                        try:
                            if await _azure_destination_ready(page, page.url or ""):
                                _emit(cb, "  ✅ MFA 验证通过，已进入 Azure 业务页面")
                                return secret
                        except Exception:
                            pass
                else:
                    consecutive_totp_fails += 1
                    # 尝试点击「切换其他验证方式」或「使用验证码」
                    for sw_sel in [
                        "a:has-text('Enter a code from an authenticator app')",
                        "button:has-text('Enter a code from an authenticator app')",
                        "a:has-text('Use a verification code')",
                        "button:has-text('Use a verification code')",
                        "a:has-text('使用验证码')",
                        "a:has-text('Sign in another way')",
                        "a:has-text('其他登录方式')",
                        "a:has-text('I want to use a different authenticator app')",
                        "button:has-text('I want to use a different authenticator app')",
                        "a:has-text('Set up a different authentication app')",
                        "a:has-text('Can\\'t scan image?')",
                        "a:has-text('Can\\'t scan the QR code?')",
                    ]:
                        try:
                            sw_loc = page.locator(sw_sel).first
                            if await sw_loc.is_visible(timeout=100):
                                await sw_loc.click(timeout=1500, force=True)
                                _emit(cb, f"  🔀 尝试切换验证选项: {sw_sel}")
                                await asyncio.sleep(0.8)
                                break
                        except Exception:
                            pass

                    if consecutive_totp_fails >= 3:
                        _emit(cb, f"  ⚠️ 连续 {consecutive_totp_fails} 次填码未就绪，尝试推进页面...")
                        await _click_first_visible(page, [
                            "button:has-text('Next')", "input[value='Next']",
                            "button:has-text('下一步')", "input[value='下一步']",
                            "button#idSIButton9", "input#idSIButton9",
                            "button#idSubmit_SAOTCC_Continue"
                        ], timeout=1500)
                        if consecutive_totp_fails >= 6:
                            _emit(cb, "  ⚠️ 连续多次无法填入 TOTP，可能页面处于其他未决状态，继续探测...")
                            consecutive_totp_fails = 0
            continue

        if state == "done":
            _emit(cb, "  🏁 MFA 注册成功，点击 Next/Done/完成...")
            await _click_first_visible(page, [
                "button#idSubmit_SAOTCC_Continue", "input#idSubmit_SAOTCC_Continue",
                "button#idSIButton9", "input#idSIButton9",
                "button:has-text('Next')", "input[value='Next']",
                "button:has-text('Done')", "input[value='Done']",
                "button:has-text('Finish')", "button:has-text('Continue')",
                "button:has-text('完成')", "button:has-text('下一步')"
            ], timeout=3000)
            await asyncio.sleep(1)
            for _w in range(12):
                if await _azure_destination_ready(page, page.url or ""):
                    _emit(cb, "  ✅ MFA 验证通过，已进入 Azure 业务页面")
                    return secret
                cur_url = (page.url or "").lower()
                cur_title = ""
                try:
                    cur_title = (await page.title()).lower()
                except Exception:
                    pass
                if "kmsi" in cur_url or "stay signed in" in cur_title or "保持登录" in cur_title:
                    break
                await asyncio.sleep(0.3)
            continue

        if state == "kmsi":
            _emit(cb, "  ✅ 点击「保持登录 (Yes)」...")
            try:
                cb_loc = page.locator("input#KmsiCheckboxField, input[name='DontShowAgain'], input[type='checkbox']").first
                if await cb_loc.is_visible(timeout=300):
                    await cb_loc.check()
            except Exception:
                pass

            await _click_first_visible(page, [
                "input#idSIButton9", "button#idSIButton9",
                "input[type='submit'][value='Yes']", "button:has-text('Yes')",
                "input[type='submit'][value='是']", "button:has-text('是')",
                "input[type='submit']", "button[type='submit']"
            ], timeout=3000)

            for _w in range(20):
                await asyncio.sleep(0.3)
                if await _azure_destination_ready(page, page.url or ""):
                    _emit(cb, "  ✅ 已进入 Azure 业务页面")
                    return secret
            continue

    return secret


# ── 点击「Pick an account」中已登录的账号 ────────────────────
async def _click_signed_in_account(page: Page, ms_email: str, cb: ProgressCallback) -> bool:
    """
    在「Pick an account」页面点击已登录的账号（Signed in 标签）。
    优先精确匹配邮箱，找不到则点第一个带「Signed in」的账号行。
    """
    email_lower = ms_email.lower()

    # 策略1: JS 按高度过滤，找包含邮箱且高度在账号行范围内的元素
    try:
        clicked = await page.evaluate("""
            (email) => {
                const skip = ['use another account', 'use a different account',
                              'sign in with', 'add account'];
                const candidates = [];
                document.querySelectorAll('div, li, a').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    const h = rect.height;
                    if (h < 35 || h > 130) return;
                    const txt = (el.innerText || '').toLowerCase();
                    if (!txt.includes(email.toLowerCase())) return;
                    if (skip.some(s => txt.includes(s))) return;
                    candidates.push({el, h});
                });
                if (!candidates.length) return false;
                candidates.sort((a, b) => a.h - b.h);
                candidates[0].el.click();
                return true;
            }
        """, email_lower)
        if clicked:
            _emit(cb, f"  ✅ 已点击账号（邮箱匹配）: {ms_email}")
            return True
    except Exception:
        pass

    # 策略2: 找带「Signed in」标签的账号行
    try:
        clicked = await page.evaluate("""
            () => {
                // 找含「Signed in」文字的小标签，向上找高度合适的行
                for (const el of document.querySelectorAll('*')) {
                    const t = (el.innerText || '').trim().toLowerCase();
                    if ((t === 'signed in' || t === '已登录') && el.offsetHeight < 30) {
                        let cur = el.parentElement;
                        for (let i = 0; i < 8; i++) {
                            if (!cur) break;
                            const h = cur.offsetHeight;
                            if (h > 35 && h < 130) { cur.click(); return true; }
                            cur = cur.parentElement;
                        }
                    }
                }
                return false;
            }
        """)
        if clicked:
            _emit(cb, "  ✅ 已点击账号（Signed in 标签匹配）")
            return True
    except Exception:
        pass

    # 策略3: Microsoft 标准 tile
    try:
        tiles = page.locator("[data-test-id='tile']")
        count = await tiles.count()
        for i in range(min(count, 5)):
            tile = tiles.nth(i)
            txt = (await tile.inner_text(timeout=500) or "").lower()
            if "use another" in txt or "use a different" in txt:
                continue
            if email_lower in txt or "signed in" in txt:
                await tile.scroll_into_view_if_needed()
                await tile.click(force=True)
                _emit(cb, "  ✅ 已点击账号（tile 匹配）")
                return True
    except Exception:
        pass

    # 策略4: 点第一个非「Use another account」的账号行
    try:
        clicked = await page.evaluate("""
            () => {
                const skip = ['use another account', 'use a different account'];
                const tiles = document.querySelectorAll('[data-test-id="tile"]');
                for (const t of tiles) {
                    const txt = (t.innerText || '').toLowerCase();
                    if (!skip.some(s => txt.includes(s))) {
                        t.click(); return true;
                    }
                }
                return false;
            }
        """)
        if clicked:
            _emit(cb, "  ✅ 已点击第一个账号（兜底）")
            return True
    except Exception:
        pass

    _emit(cb, "  ⚠️  未能点击账号选择器中的账号")
    return False


# ── 主登录流程 ────────────────────────────────────────────────
async def do_azure_login(
    page: Page,
    ctx: BrowserContext,
    ms_email: str,
    ms_password: str,
    cb: ProgressCallback = None,
    target_url: str | None = None,
    existing_totp_secret: str = "",
    proxy_ctrl: Optional[Any] = None,
) -> str:
    """
    完整 Azure 微软账号登录流程。
    支持自定义目标入口 URL (如直接打开 Education Software)，
    支持已有 TOTP 密钥自动填码，并可自动完成新 MFA 注册。
    返回 totp_secret（若触发了 MFA 注册）或原始 secret。
    """
    _emit(cb, f"  🔑 开始微软账号登录: {ms_email}")

    # ── 第一步：打开目标入口 URL ─────────────
    default_login_target = getattr(config, "AZURE_SIGNUP_LOGIN_URL", config.AZURE_SIGNUP_URL)
    target_url = target_url or default_login_target
    _emit(cb, f"  🌐 打开 {target_url[:80]}")
    for attempt in range(3):
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            if attempt < 2:
                _emit(cb, f"  ⚠️  导航失败（第{attempt+1}次）: {str(e)[:60]}，重试...")
                await asyncio.sleep(2)
            else:
                raise LoginNetworkError(f"导航目标入口失败: {e}")

    # 导航后等待登录页出现（轮询，等待页面重定向至微软登录页）
    for _w in range(30):
        await asyncio.sleep(0.5)
        url = page.url
        if any(k in url for k in _LOGIN_URL_KEYS) or any(k in url for k in _PORTAL_URL_KEYS):
            break
        try:
            if await page.locator(SEL_EMAIL).first.is_visible(timeout=200):
                break
        except Exception:
            pass

    # 等待登录页出现
    for _ in range(20):
        url = page.url
        if any(k in url for k in _LOGIN_URL_KEYS):
            break
        try:
            if await page.locator(SEL_EMAIL).first.is_visible(timeout=1000):
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    # ── 账号选择器：点「Use a different account」────────────────
    try:
        body = await page.locator("body").inner_text(timeout=3000)
        if "Pick an account" in body or "Choose an account" in body or "选择账户" in body:
            _emit(cb, "  🔀 出现账号选择器，点「Use a different account」...")
            await _click_first_visible(page, [
                "a:has-text('Use a different account')",
                "div[data-test-id='tile']:has-text('Use a different account')",
                "button:has-text('Use a different account')",
                "a:has-text('Sign in with a different account')",
            ], timeout=8000)
            # 等待邮箱输入框出现
            for _w in range(16):
                await asyncio.sleep(0.25)
                try:
                    if await page.locator(SEL_PASS).first.is_visible(timeout=200):
                        break
                except Exception:
                    pass
    except Exception:
        pass

    # ── 输入邮箱 ──────────────────────────────────────────────
    email_submitted = False
    for email_attempt in range(3):
        try:
            email_loc = page.locator(SEL_EMAIL).first
            await email_loc.wait_for(state="visible", timeout=15000)
            if email_attempt == 0:
                _emit(cb, f"  📧 输入邮箱: {ms_email}")
                # 关键：冷启动与初始页面等待后台安全遥测 (Arkose/FPT/ASM) 充分初始化
                await human_settle(page, min_s=1.2, max_s=2.2, target_locator=email_loc)

            await human_type(page, email_loc, ms_email, min_delay=0.04, max_delay=0.10)
            email_url = page.url
            await asyncio.sleep(random.uniform(0.35, 0.65))

            # 拟人化自然点击 Next / 下一步 按钮提交邮箱（避免 Enter + Click 双重并发触发风控）
            next_btn_clicked = await _click_first_visible(page, [
                "input#idSIButton9", "button#idSIButton9",
                "input[type='submit'][value='Next']", "button:has-text('Next')",
                "input[type='submit'][value='下一步']", "button:has-text('下一步')"
            ], timeout=3000)
            if not next_btn_clicked:
                await email_loc.press("Enter")
            # 等待邮箱提交后的跳转或密码框出现（轮询，最多3秒）
            for _w in range(12):
                await asyncio.sleep(0.25)
                try:
                    if page.url != email_url:
                        break
                    if await _first_visible_locator(page, SEL_PASS) is not None:
                        break
                except Exception:
                    pass
            email_submitted = True
            break
        except Exception as e:
            is_timeout = isinstance(e, PWTimeout) or "Timeout" in type(e).__name__ or "Timeout" in str(e)
            if email_attempt < 2:
                if is_timeout:
                    _emit(cb, f"  ⚠️  等待邮箱输入框响应超时，重试 ({email_attempt + 2}/3)...")
                else:
                    _emit(cb, f"  ⚠️  邮箱输入异常 ({str(e)[:40]})，重试 ({email_attempt + 2}/3)...")
                await asyncio.sleep(0.5)
            else:
                if is_timeout or "Timeout" in str(e):
                    raise EmailInputTimeoutError(f"无法输入邮箱（页面未加载或输入超时）: {e}")
                else:
                    raise RuntimeError(f"无法输入邮箱: {e}")

    # ── 输入密码（兼容微软页跳转到学校 SSO 页）────────────────
    submitted_password_forms: set[str] = set()
    password_submitted = False
    no_password_polls = 0
    try:
        for _w in range(120):  # 最多等待 60 秒
            if await _submit_visible_password(
                page, ms_password, submitted_password_forms, cb
            ):
                password_submitted = True
                no_password_polls = 0
                await asyncio.sleep(0.5)
                continue

            # 若密码已提交，高频检测页面是否有明确的登录报错 (如账号被锁、密码错误等)，毫秒级快速报错
            if password_submitted:
                ms_err = await _get_ms_login_error(page)
                if ms_err:
                    raise RuntimeError(f"微软登录失败: {ms_err}")

            visible_password = await _first_visible_locator(page, SEL_PASS)
            if password_submitted and visible_password is None:
                no_password_polls += 1
                # 密码框连续消失 1 秒后，进入登录中间页处理。
                if no_password_polls >= 2:
                    break
            else:
                no_password_polls = 0

            # 已有有效登录会话时可能完全不出现密码框。
            if (
                not password_submitted
                and any(k in page.url for k in _PORTAL_URL_KEYS)
                and _w >= 5
            ):
                break
            await asyncio.sleep(0.5)
        else:
            ms_err = await _get_ms_login_error(page)
            if ms_err:
                raise RuntimeError(f"微软登录失败: {ms_err}")
            raise RuntimeError("等待密码页或密码提交完成超时")
    except Exception as e:
        raise RuntimeError(f"无法输入密码: {e}")

    # ── 处理登录后各种中间页 ──────────────────────────────────
    totp_secret = existing_totp_secret or ""
    _POPUP_KEYS = ("proofs", "recover", "kmsi", "protection",
                   "login.live", "login.microsoftonline", "ppsecure",
                   "mysignins.microsoft.com")

    azure_ready_streak = 0
    login_completed = False
    login_last_snapshot = ""
    login_last_change = asyncio.get_event_loop().time()
    login_refresh_count = 0

    for attempt in range(60):
        await asyncio.sleep(0.5)
        url = page.url
        url_lower = url.lower()

        # 读取页面文本用于 Fast-Fail 校验
        try:
            body = await page.locator("body").inner_text(timeout=1000)
            body_lower = body.lower()
        except Exception:
            body, body_lower = "", ""

        # 精确监测 DOM 元素中的登录错误提示 (Fast-Fail 毫秒级中断)
        ms_err = await _get_ms_login_error(page)
        if ms_err:
            raise RuntimeError(f"微软登录失败: {ms_err}")

        # 1. 人机验证 (CAPTCHA / Robot Puzzle) 穿透式高精度识别
        is_captcha, cap_msg = await check_captcha_present(page)
        if is_captcha:
            raise AzureCaptchaError(f"触发 Azure 人机拼图验证 ({cap_msg})")

        if ("password is incorrect" in body_lower or "incorrect password" in body_lower or
                "account or password is incorrect" in body_lower or "your account has been locked" in body_lower or
                "account is locked" in body_lower or "密码不正确" in body_lower or "密码错误" in body_lower):
            raise RuntimeError(f"账号密码错误或被封禁: {ms_email}")

        if ("account doesn't exist" in body_lower or "no account found" in body_lower or
                "username may be incorrect" in body_lower or "账户不存在" in body_lower or "帐户不存在" in body_lower):
            raise RuntimeError(f"账号不存在: {ms_email}")

        # 检查单步骤 50 秒停滞无动静与 3 次刷新 (150s 超时放弃)
        login_snap = f"{url_lower}"
        if login_snap != login_last_snapshot:
            login_last_snapshot = login_snap
            login_last_change = asyncio.get_event_loop().time()
        else:
            stagnant_login = asyncio.get_event_loop().time() - login_last_change
            if stagnant_login >= 50 and login_refresh_count < 3:
                login_refresh_count += 1
                login_last_change = asyncio.get_event_loop().time()
                switched_p = None
                if proxy_ctrl:
                    try:
                        switched_p = await proxy_ctrl.switch_next()
                    except Exception:
                        pass
                p_msg = f"，已热切换至代理: {switched_p}" if switched_p else ""
                _emit(cb, f"  ⚠️ [网络延迟适配] 登录界面 50 秒无响应{p_msg}，尝试自动刷新网页 ({login_refresh_count}/3)...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    _emit(cb, f"  ⚠️ 自动刷新异常: {e}")
            elif stagnant_login >= 50 and login_refresh_count >= 3:
                raise RuntimeError("⚠️ 单步骤连续 50 秒无响应且自动刷新 3 次(共150s)仍无进展，放弃当前账号")

        # 确认已进入业务页面
        if await _azure_destination_ready(page, url):
            azure_ready_streak += 1
            if azure_ready_streak >= 2:
                _emit(cb, "  ✅ 已确认进入 Azure 业务页面")
                login_completed = True
                break
            continue
        azure_ready_streak = 0

        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        body_lower = body.lower()

        # 联邦账号可能在登录中间阶段才跳到学校 SSO 密码页。
        if await _submit_visible_password(
            page, ms_password, submitted_password_forms, cb
        ):
            await asyncio.sleep(0.5)
            continue

        # 0. 已有 TOTP 密钥的 2FA 动态验证码校验
        if totp_secret:
            totp_switch_sels = [
                "a:has-text('Enter a code from an authenticator app')",
                "button:has-text('Enter a code from an authenticator app')",
                "a:has-text('Already have a code?')",
                "a:has-text('Use a verification code')",
                "a:has-text('use a verification code')",
                "a:has-text('使用验证码')",
            ]
            for sw_sel in totp_switch_sels:
                try:
                    sw = page.locator(sw_sel).first
                    if await sw.is_visible(timeout=300):
                        _emit(cb, "  🔀 点击「Enter a code / 使用验证码」切换 2FA 验证方式...")
                        await sw.click(force=True)
                        await asyncio.sleep(0.8)
                        break
                except Exception:
                    pass

            code_input_sels = [
                "input[name='otc']", "input#idTxtBx_SAOTCC_OTC",
                "input[autocomplete='one-time-code']",
                "input[placeholder*='code']", "input[placeholder*='代码']",
            ]
            for sel in code_input_sels:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=500):
                        code = pyotp.TOTP(totp_secret).now()
                        _emit(cb, f"  🔐 输入已有 TOTP 验证码: {code}")
                        await loc.fill(code)
                        await _click_first_visible(page, [
                            "button#idSubmit_SAOTCC_Continue", "input#idSubmit_SAOTCC_Continue",
                            "button:has-text('Next')", "button:has-text('Verify')", "button:has-text('验证')",
                            "button:has-text('下一步')", "button[type='submit']",
                        ], timeout=5000)
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

        # ① 「Let's keep your account secure」/「需要更多信息」 → 直接点 Next / 下一步 进行 2FA 绑定
        if ("let's keep your account secure" in body_lower or
                "keep your account secure" in body_lower or
                "more information required" in body_lower or
                "保护帐户安全" in body or "保护账户安全" in body or "需要详细信息" in body or "保持账户安全" in body):
            _emit(cb, "  🔑 出现「Let's keep your account secure」界面，点「下一步/Next」开始 2FA 绑定...")
            next_clicked = await _click_first_visible(page, [
                "input#idSubmit_ProofUp_Redirect",
                "input#idSIButton9",
                "button:has-text('Next')",
                "input[value='Next']",
                "button:has-text('下一步')",
                "input[value='下一步']",
            ], timeout=8000)
            for _w in range(8):
                await asyncio.sleep(0.25)
                try:
                    if page.url != url:
                        break
                except Exception:
                    pass
            try:
                totp_secret = await _handle_mfa_setup(page, cb)
            except Exception as e:
                _emit(cb, f"  ⚠️  MFA 注册异常: {e}")
            continue

        # ② 「Pick an account」选择器 → 点已登录的账号（Signed in）
        if ("pick an account" in body_lower or "choose an account" in body_lower):
            _emit(cb, "  👆 出现账号选择器，点已登录的账号...")
            clicked = await _click_signed_in_account(page, ms_email, cb)
            if clicked:
                for _w in range(8):
                    await asyncio.sleep(0.25)
                    try:
                        if page.url != url:
                            break
                    except Exception:
                        pass
            continue

        # ③ Install Microsoft Authenticator（直接进入 MFA 页，说明账号没有已登录session）
        if "install microsoft authenticator" in body_lower or "mysignins.microsoft.com" in url_lower:
            if totp_secret:
                # 已完成 MFA 注册，直接跳转至目标页面（如学生认证登录入口）
                _emit(cb, f"  🌐 MFA 注册已完成，主动导航至目标入口: {target_url[:70]}...")
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            _emit(cb, "  🔐 直接进入 MFA 注册页...")
            try:
                totp_secret = await _handle_mfa_setup(page, cb)
            except Exception as e:
                _emit(cb, f"  ⚠️  MFA 注册异常: {e}")
            continue

        # ④ Stay signed in / KMSI / 保持登录
        if "stay signed in" in body_lower or "kmsi" in url_lower or "保持登录" in body:
            _emit(cb, "  ✅ 点击「保持登录 (Yes)」...")
            try:
                cb_loc = page.locator("input#KmsiCheckboxField, input[name='DontShowAgain'], input[type='checkbox']").first
                if await cb_loc.is_visible(timeout=300):
                    await cb_loc.check()
            except Exception:
                pass
            await _click_first_visible(page, [
                "input#idSIButton9", "button#idSIButton9",
                "input[type='submit'][value='Yes']", "button:has-text('Yes')",
                "input[type='submit'][value='是']", "button:has-text('是')",
                "input[type='submit']", "button[type='submit']",
                "input#idBtn_Back", "button#CancelButton"
            ], timeout=5000)
            await asyncio.sleep(0.5)
            continue

        # ⑤ Skip for now / 以后再说
        try:
            skip = page.locator("a:has-text('Skip for now'), a:has-text('以后再说')").first
            if await skip.is_visible(timeout=500):
                await skip.click(force=True)
                await asyncio.sleep(0.3)
                continue
        except Exception:
            pass

        # ⑥ 密码错误 / 账号被封禁
        if ("password is incorrect" in body_lower or
                "incorrect password" in body_lower or
                "account or password is incorrect" in body_lower or
                "your account has been locked" in body_lower or
                "account is locked" in body_lower or
                "密码不正确" in body_lower or "密码错误" in body_lower):
            raise RuntimeError(f"账号密码错误或被封禁: {ms_email}")

        # ⑦ 账号不存在
        if ("account doesn't exist" in body_lower or
                "no account found" in body_lower or
                "username may be incorrect" in body_lower or
                "账户不存在" in body_lower or "帐户不存在" in body_lower):
            raise RuntimeError(f"账号不存在: {ms_email}")

        if attempt % 5 == 0:
            _emit(cb, f"  ⏳ [{attempt+1}/60] 等待登录完成... URL: {url[:70]}")

    if not login_completed:
        _emit(cb, "  ⚠️  登录/MFA 跳转等待超时，尝试主动刷新页面重新检测业务界面 (1/2)...")
        for refresh_idx in range(2):
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                _emit(cb, f"  🔄 网页刷新完成 (第{refresh_idx+1}次)，检测界面状态...")
                await asyncio.sleep(1.5)
                for _ in range(12):
                    if await _azure_destination_ready(page, page.url):
                        _emit(cb, f"  ✅ 刷新后已确认进入 Azure 业务页面 (第{refresh_idx+1}次刷新成功)")
                        login_completed = True
                        break
                    await asyncio.sleep(0.5)
                if login_completed:
                    break
                if refresh_idx == 0:
                    _emit(cb, "  ⚠️  首次刷新后仍未确认，尝试第二次刷新 (2/2)...")
            except Exception as e:
                _emit(cb, f"  ⚠️ 自动刷新异常: {e}")

    if not login_completed:
        raise RuntimeError(f"微软账号登录未完成，当前页面: {page.url[:180]}")

    _emit(cb, f"  ✅ 微软账号登录完成，TOTP secret: {'已获取/已有' if totp_secret else '无需注册'}")
    return totp_secret

