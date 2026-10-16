"""
Azure for Students — Academic Verification 表单填写
-----------------------------------------------------
页面: signup.azure.com/studentverification (SheerID sse-vnext 表单)

字段说明:
  - First name        (#sse-vnext-fname-input)  → 随机英文名
  - Last name         (#sse-vnext-lname-input)  → 随机英文姓
  - Country           (select)                  → United States（默认不动）
  - School name       (可选，留空)
  - Date of birth     (#sse-vnext-dob-input)    → 随机生日（必填，JS赋值）
  - School email      (#sse-vnext-email-input)  → 已自动填好登录邮箱，不需要动

流程:
  1. 支持最多 3 次自动刷新重试 (max_attempts = 3)
  2. 等待 SheerID 表单渲染（#sse-vnext-fname-input 出现，单次最多 20s）
  3. 填 First name / Last name / Date of birth
  4. School email 已自动填好，验证一下即可，不重复填写
  5. 等待 Verify academic status 按钮变蓝（Turnstile 通过，最多 90s）
  6. 点击提交，等待页面响应（confirming your account，最多 15s）
  7. 结果确认等待（最多 60s），若成功则放行提 Key；若超时则自动刷新页面重试！
"""

import asyncio
import logging
import os
import random
import sys
import uuid
from urllib.parse import urlsplit
from typing import Callable, Optional

from playwright.async_api import Page

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.captcha_detector import check_captcha_present
from utils.human_action import (
    human_bezier_move,
    human_settle,
    human_hover_and_click,
    human_type,
)

log = logging.getLogger(__name__)
ProgressCallback = Optional[Callable[[str], None]]


class SheerIDVerificationError(RuntimeError):
    """SheerID 学术核验在当前浏览器会话中重试均未通过。"""
    pass


class AzureCaptchaError(RuntimeError):
    """触发 Azure / Arkose / SheerID 人机拼图验证码。"""
    pass


_FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Emily", "Sarah", "Jessica", "Amanda",
    "Melissa", "Jennifer", "Elizabeth", "Ashley", "Daniel", "Matthew",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Jackson", "White", "Harris",
    "Martin", "Thompson", "Moore", "Young", "Walker", "Allen",
]

_FN_SELECTORS = [
    "#sse-vnext-fname-input", "#first-name-input", "#firstName-input", "#firstname",
    "input[name*='first' i]", "input[name*='fname' i]",
    "input[id*='fname' i]", "input[id*='first' i]",
    "input[placeholder*='First' i]", "input[aria-label*='First' i]",
    "input[data-testid*='first' i]"
]

_LN_SELECTORS = [
    "#sse-vnext-lname-input", "#last-name-input", "#lastName-input", "#lastname",
    "input[name*='last' i]", "input[name*='lname' i]",
    "input[id*='lname' i]", "input[id*='last' i]",
    "input[placeholder*='Last' i]", "input[aria-label*='Last' i]",
    "input[data-testid*='last' i]"
]

_DOB_SELECTORS = [
    "#sse-vnext-dob-input", "#birth-date-input", "#dob-input", "#birthdate",
    "input[type='date']", "input[name*='birth' i]", "input[name*='dob' i]",
    "input[id*='dob' i]", "input[id*='birth' i]",
    "input[placeholder*='birth' i]", "input[placeholder*='dob' i]",
    "input[aria-label*='birth' i]", "input[aria-label*='dob' i]"
]

_COUNTRY_SELECTORS = [
    "#sse-vnext-country-input", "select[name*='country' i]",
    "select[id*='country' i]", "select"
]

_EMAIL_SELECTORS = [
    "#sse-vnext-email-input", "input[placeholder*='school email' i]",
    "input[aria-label*='school email' i]", "input[placeholder*='email' i]",
    "input[name*='email' i]", "input[id*='email' i]", "input[type='email']"
]

_SUBMIT_SELECTORS = [
    "button:has-text('Next'):not(:has-text('accept'))", "button:has-text('下一步'):not(:has-text('条款'))",
    "#sse-vnext-submit-button",
    "button:has-text('Verify academic status'):not(:has-text('accept terms'))",
    "button:has-text('验证学术状态'):not(:has-text('条款'))",
    "button:has-text('Verify eligibility')", "button:has-text('Verify status')",
    "button:has-text('Verify'):not(:has-text('accept'))",
    "button:has-text('验证'):not(:has-text('条款'))",
    "button[type='submit']", "input[type='submit']"
]

_TERMS_SUBMIT_SELECTORS = [
    "button:has-text('Verify academic status and accept terms')",
    "button:has-text('验证学术地位并接受条款')",
    "button:has-text('accept terms')",
    "button:has-text('接受条款')",
    "button:has-text('Verify academic status')",
    "button:has-text('验证学术状态')",
    "button[type='submit']",
]

_TRIGGER_BUTTON_SELECTORS = [
    "button:has-text('Verify academic status')", "button:has-text('Verify eligibility')",
    "button:has-text('Start verification')", "button:has-text('Check eligibility')",
    "button:has-text('验证学术状态')", "button:has-text('开始验证')",
    "button:has-text('Get started')", "button:has-text('开始使用')",
    "button:has-text('Next')", "button:has-text('下一步')", "button:has-text('Continue')"
]

_btn_js = """() => {
    // 查找第一步提交按钮（Next / Verify academic status），严格排除含 "accept terms" 的第二步按钮
    function isStep1Btn(el) {
        const txt = (el.textContent || el.value || '').trim();
        if (txt.toLowerCase().includes("accept terms") || txt.includes("接受条款")) return false;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return false;
        return txt.includes("Verify academic status") || txt.includes("验证学术状态") ||
               txt.includes("Verify eligibility") || txt.includes("Verify status") ||
               txt.includes("Verify") || txt.includes("验证") ||
               txt === "Next" || txt === "下一步" || txt.includes("Next") || txt.includes("下一步") ||
               txt.includes("Submit");
    }
    const sseBtn = document.querySelector("#sse-vnext-submit-button");
    const btn = (sseBtn && isStep1Btn(sseBtn)) ? sseBtn :
                [...document.querySelectorAll("button, input[type='submit']")].find(isStep1Btn);
    if (!btn) return "not_found";
    const r = btn.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return "hidden";
    if (btn.disabled) return "disabled";
    if (btn.getAttribute("aria-disabled") === "true") return "disabled";
    const style = window.getComputedStyle(btn);
    if (style.pointerEvents === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") < 0.6) return "disabled";

    // 严格检测背景颜色是否已变蓝（无论 Next 还是 Verify，均需变蓝后才可点击）
    const bg = (style.backgroundColor || '').toLowerCase();
    const isWhiteOrTransparent = bg === 'rgb(255, 255, 255)' || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent' || bg.includes('255, 255, 255');
    if (isWhiteOrTransparent) {
        return "not_blue_yet";
    }

    const match = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    if (match) {
        const rVal = parseInt(match[1]), gVal = parseInt(match[2]), bVal = parseInt(match[3]);
        // 浅色或浅灰底表示尚未激活变蓝 (白色/浅灰底)
        if (rVal > 180 && gVal > 180 && bVal > 180) {
            return "not_blue_yet";
        }
        // 纯灰底 (r, g, b 互相接近) 尚未变蓝
        if (Math.abs(rVal - gVal) < 20 && Math.abs(gVal - bVal) < 20 && bVal < 200) {
            return "not_blue_yet";
        }
        // 蓝色系：蓝色分量明显高于红色和绿色，且 bVal > 120
        if (bVal > 120 && bVal > rVal + 20) {
            return "enabled";
        }
    }

    // 类名辅助判断：若明确包含 primary / btn-blue / active 且没有 disabled 类
    const cls = (btn.className || '').toLowerCase();
    if ((cls.includes('primary') || cls.includes('blue') || cls.includes('active')) && !cls.includes('disabled')) {
        return "enabled";
    }

    return "not_blue_yet";
}"""


def _emit(cb: ProgressCallback, msg: str):
    log.info(msg)
    if cb:
        cb(msg)


async def _find_visible_locator(page: Page, selectors: list[str], timeout_ms: int = 150):
    """穿透页面及所有子 iframe，查找第一个可见的目标元素。"""
    targets = list(page.frames)
    if page.main_frame not in targets:
        targets.insert(0, page.main_frame)
    sel_query = ", ".join(selectors)
    for tgt in targets:
        try:
            matches = tgt.locator(sel_query)
            for index in range(await matches.count()):
                loc = matches.nth(index)
                if await loc.is_visible(timeout=timeout_ms):
                    return tgt, loc
        except Exception:
            pass
    return None, None


async def _complete_azure_offer_signup(page: Page, ms_email: str, cb: ProgressCallback = None) -> bool:
    """The signup.azure.com/signup profile page means SheerID passed; the form needs no input
    and key extraction opens its own tab."""
    _emit(cb, "  ✅ 已到达订阅资料页（无需填写），SheerID 认证通过，转入新标签页提取 Key...")
    return True


async def _handle_terms_acceptance_if_present(page: Page, cb: ProgressCallback = None, max_wait_s: float = 10.0) -> bool:
    """
    自适应检测并处理条款确认卡片（Terms）：
    - 适配部分账号在提交基本学术信息后展开的第二步「Terms / 条款」卡片；
    - 若账号无此步骤（旧版账号直接跳转或进入确认），平滑返回 False；
    - 若出现条款卡片：
      1. 精确勾选第一个同意条款（我同意订阅协议、Azure Dev Tools for Teaching 协议）；
      2. 轮询等待最下方的验证按钮（Verify academic status and accept terms）激活变蓝；
      3. 按钮变蓝后，使用拟人化方式点击提交。
    """
    start_t = asyncio.get_event_loop().time()
    targets = [page] + list(page.frames)
    terms_detected = False

    while (asyncio.get_event_loop().time() - start_t) < max_wait_s:
        # 0. 快速退出判断：若已脱离 studentverification 页面，说明直接通过了
        cur_url = (page.url or "").lower()
        if "portal.azure.com" in cur_url or "education.azure.com" in cur_url or ("signup.azure.com/signup" in cur_url and "/studentverification" not in cur_url):
            return False

        try:
            body_txt = (await page.locator("body").inner_text(timeout=300)).lower()
            # 仅在明确进入账号确认/设置阶段时退出（排除 Turnstile 的 "验证完成" 小徽章）
            if any(k in body_txt for k in ("confirming your account", "正在确认你的帐户", "setting up your account")):
                return False
        except Exception:
            body_txt = ""

        # 检查是否出现 Terms 相关元素或文本（含展开的条款复选框区域）
        for tgt in targets:
            try:
                has_terms = await tgt.evaluate("""() => {
                    const txt = (document.body ? document.body.innerText : '').toLowerCase();
                    // 检测条款关键词（subscription agreement 只在 Terms 展开后可见）
                    const hasAgreement = txt.includes('subscription agreement') || txt.includes('订阅协议') ||
                                         txt.includes('azure dev tools for teaching') || txt.includes('i agree to');
                    // 检测 accept terms 按钮
                    const btns = [...document.querySelectorAll('button, input[type="submit"]')];
                    const hasTermsBtn = btns.some(b => {
                        const t = (b.textContent || b.value || '').trim().toLowerCase();
                        return t.includes('accept terms') || t.includes('接受条款') ||
                               (t.includes('verify') && t.includes('terms')) ||
                               (t.includes('验证') && t.includes('条款'));
                    });
                    // 检测复选框（兼容原生 <input> 和 ARIA [role="checkbox"]）
                    const cbCount = document.querySelectorAll('input[type="checkbox"], [role="checkbox"]').length;
                    // 条件：有订阅协议文本 或 (有条款按钮 且 有复选框)
                    return hasAgreement || (hasTermsBtn && cbCount >= 1) || ((txt.includes('terms') || txt.includes('条款')) && cbCount >= 2);
                }""")
                if has_terms:
                    terms_detected = True
                    break
            except Exception:
                pass

        if terms_detected:
            break
        await asyncio.sleep(0.4)

    if not terms_detected:
        return False

    _emit(cb, "  📋 检测到条款确认面板 (Terms)，开始处理协议勾选...")

    # ── 1. 勾选第一个同意条款（订阅协议、Azure Dev Tools for Teaching 协议） ──
    cb_checked = False
    for attempt in range(6):
        for tgt in targets:
            try:
                res = await tgt.evaluate("""() => {
                    const cbs = [...document.querySelectorAll('input[type="checkbox"], [role="checkbox"]')].filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    if (cbs.length === 0) return { status: 'no_cb' };

                    // 优先寻找文本包含订阅协议 (subscription agreement) 的第一个 checkbox
                    let targetCb = null;
                    for (const cb of cbs) {
                        let p = cb.closest('label') || cb.parentElement;
                        for (let depth = 0; depth < 5 && p; depth++) {
                            const t = (p.innerText || p.textContent || '').toLowerCase();
                            if (t.includes('subscription agreement') || t.includes('订阅协议') || t.includes('dev tools') || t.includes('我同意') || t.includes('i agree')) {
                                targetCb = cb;
                                break;
                            }
                            p = p.parentElement;
                        }
                        if (targetCb) break;
                    }

                    // 兜底：直接取可见的第一个复选框（截图明确展示第1个即为条款）
                    if (!targetCb) {
                        targetCb = cbs[0];
                    }

                    const isChecked = targetCb.checked || targetCb.getAttribute('aria-checked') === 'true';
                    if (!isChecked) {
                        const label = targetCb.closest('label') || (targetCb.id ? document.querySelector('label[for="' + targetCb.id + '"]') : null);
                        if (label) {
                            label.click();
                        } else {
                            targetCb.click();
                        }
                        if (!targetCb.checked && targetCb.getAttribute('aria-checked') !== 'true') {
                            targetCb.checked = true;
                            targetCb.setAttribute('aria-checked', 'true');
                            targetCb.dispatchEvent(new Event('change', { bubbles: true }));
                            targetCb.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    }
                    return { status: 'ok', checked: targetCb.checked || targetCb.getAttribute('aria-checked') === 'true' };
                }""")
                if res and res.get("checked"):
                    cb_checked = True
                    break
            except Exception:
                pass

        if cb_checked:
            break
        await asyncio.sleep(0.4)

    if cb_checked:
        _emit(cb, "  📝 已成功勾选第一个同意条款 (我同意订阅协议、Azure Dev Tools for Teaching 协议)")
    else:
        _emit(cb, "  ⚠️ 未能确凿勾选协议复选框，继续检查按钮状态...")

    await asyncio.sleep(random.uniform(0.4, 0.7))

    # ── 2. 等待最下方的验证按钮激活变蓝 (enabled) ──
    _emit(cb, "  ⏳ 等待「验证学术地位并接受条款」按钮激活变蓝...")
    btn_ready = False

    for _step in range(25):  # 最多等待约 12.5 秒
        for tgt in targets:
            try:
                state = await tgt.evaluate("""() => {
                    const btns = [...document.querySelectorAll("button, input[type='submit']")];
                    const btn = btns.find(b => {
                        const t = (b.textContent || b.value || '').trim().toLowerCase();
                        return t.includes('accept terms') || t.includes('接受条款') ||
                               (t.includes('verify') && t.includes('terms')) ||
                               (t.includes('验证') && t.includes('条款'));
                    }) || btns.find(b => {
                        const t = (b.textContent || b.value || '').trim().toLowerCase();
                        return t.includes('verify') || t.includes('验证');
                    });
                    if (!btn) return "not_found";

                    const r = btn.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return "hidden";
                    if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return "disabled";

                    const style = window.getComputedStyle(btn);
                    if (style.pointerEvents === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") < 0.6) return "disabled";

                    // 检查背景色：白底或浅底表示尚未变蓝
                    const bg = (style.backgroundColor || '').toLowerCase();
                    const isWhite = bg === 'rgb(255, 255, 255)' || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent' || bg.includes('255, 255, 255');
                    if (isWhite) {
                        return "white_not_ready";
                    }

                    const match = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                    if (match) {
                        const rVal = parseInt(match[1]), gVal = parseInt(match[2]), bVal = parseInt(match[3]);
                        if (rVal > 220 && gVal > 220 && bVal > 220) {
                            return "white_not_ready";
                        }
                        if (bVal > 120 && bVal > rVal) {
                            return "blue_ready";
                        }
                    }

                    return "ready";
                }""")
                if state in ("blue_ready", "ready"):
                    btn_ready = True
                    break
            except Exception:
                pass

        if btn_ready:
            break
        await asyncio.sleep(0.5)

    if not btn_ready:
        _emit(cb, "  ⚠️ 条款验证按钮未明确检测到变蓝，尝试拟人化直接点击...")
    else:
        _emit(cb, "  🔵 「验证学术地位并接受条款」按钮已变蓝激活")

    await asyncio.sleep(random.uniform(0.3, 0.6))

    # ── 3. 点击「验证学术地位并接受条款」按钮 ──
    clicked = False
    tgt_tbtn, t_btn_loc = await _find_visible_locator(page, _TERMS_SUBMIT_SELECTORS, timeout_ms=1500)
    if t_btn_loc:
        try:
            await t_btn_loc.scroll_into_view_if_needed()
            await human_hover_and_click(page, t_btn_loc, timeout=4000)
            clicked = True
        except Exception:
            try:
                await t_btn_loc.click(timeout=3000)
                clicked = True
            except Exception:
                pass

    if not clicked:
        for tgt in targets:
            try:
                ok = await tgt.evaluate("""() => {
                    const btns = [...document.querySelectorAll("button, input[type='submit']")];
                    const btn = btns.find(b => {
                        const t = (b.textContent || b.value || '').trim().toLowerCase();
                        return t.includes('accept terms') || t.includes('接受条款') ||
                               (t.includes('verify') && t.includes('terms')) ||
                               (t.includes('验证') && t.includes('条款'));
                    });
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                }""")
                if ok:
                    clicked = True
                    break
            except Exception:
                pass

    _emit(cb, "  🎓 点击「验证学术地位并接受条款」(Verify academic status and accept terms)")
    return True


async def fill_azure_profile_form(page: Page, ms_email: str, cb: ProgressCallback = None) -> bool:
    """保留有界加载诊断，结束时移除监听，避免长时间运行积累回调。"""
    diagnostics = []

    def record(kind, request, detail):
        if len(diagnostics) >= 12 or request.resource_type not in ('document', 'script', 'xhr', 'fetch'):
            return
        parsed = urlsplit(request.url)
        # 只保留主机与路径，不记录 query、请求体、Cookie 或 token。
        entry = f"{kind} {detail} {parsed.hostname or '-'}{parsed.path[:140]}"
        if entry not in diagnostics:
            diagnostics.append(entry)

    def failed(request):
        record('requestfailed', request, request.failure or 'unknown')

    def response_received(response):
        if response.status >= 400:
            record('HTTP', response.request, str(response.status))

    def page_error(error):
        if len(diagnostics) < 12:
            diagnostics.append(f"pageerror {getattr(error, 'name', type(error).__name__)}")

    listeners = [('requestfailed', failed), ('response', response_received), ('pageerror', page_error)]
    for event, callback in listeners:
        page.on(event, callback)
    try:
        return await _fill_azure_profile_form(page, ms_email, cb, diagnostics)
    finally:
        for event, callback in listeners:
            page.remove_listener(event, callback)


async def _fill_azure_profile_form(page: Page, ms_email: str, cb, diagnostics) -> bool:
    """
    填写 Academic Verification 表单并提交，支持单浏览器内 1 次刷新重试机制 (max_attempts = 2)。
    必须确凿确认认证成功后才返回 True；若尝试均超时则抛出 SheerIDVerificationError 触发新开浏览器重试。
    """
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        _emit(cb, f"  📝 执行 SheerID 学术资格认证 (尝试 {attempt}/{max_attempts})...")

        # 1. 快速检查是否已在 Portal / 后续成功页面
        cur_url = (page.url or "").lower()
        if "portal.azure.com" in cur_url or "education.azure.com" in cur_url:
            is_unregistered_portal = False
            try:
                is_unregistered_portal = await page.evaluate("""
                    () => {
                        const txt = (document.body ? document.body.innerText : '').toLowerCase();
                        return txt.includes('sign up for azure for students') ||
                               (txt.includes('sign up now') && txt.includes('students')) ||
                               (txt.includes('面向学生的 azure') && txt.includes('立即注册'));
                    }
                """)
            except Exception:
                pass
            if is_unregistered_portal:
                _emit(cb, "  ⚠️ 当前处于 Azure Portal 但尚未开通学生资格（提示需 Sign up now），继续导航至注册表单...")
                import config
                try:
                    await page.goto(config.AZURE_SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            else:
                _emit(cb, "  ✅ 学术认证已通过，无需重复填写")
                return True
        elif "signup.azure.com/signup" in cur_url and "/studentverification" not in cur_url:
            _emit(cb, "  ✅ 学术认证已通过，处于订阅激活阶段，开始完成订阅...")
            return await _complete_azure_offer_signup(page, ms_email, cb)

        # 2. 等待 SheerID 表单渲染。慢网络下 Angular/iframe 可能需要几十秒，
        #    不在等待中途刷新，避免把正在加载的表单主动打断。
        _emit(cb, "  📝 等待 Academic Verification 表单加载...")
        form_found = False
        active_target = page

        form_wait_seconds = 20
        for wait_s in range(form_wait_seconds):
            cur_url = (page.url or "").lower()
            if "signup.azure.com/signup" in cur_url and "/studentverification" not in cur_url:
                _emit(cb, "  ✅ 学术认证已通过，进入订阅激活阶段...")
                return await _complete_azure_offer_signup(page, ms_email, cb)
            if "portal.azure.com" in cur_url or "education.azure.com" in cur_url:
                is_unregistered_portal = False
                try:
                    is_unregistered_portal = await page.evaluate("""
                        () => {
                            const txt = (document.body ? document.body.innerText : '').toLowerCase();
                            return txt.includes('sign up for azure for students') ||
                                   (txt.includes('sign up now') && txt.includes('students')) ||
                                   (txt.includes('面向学生的 azure') && txt.includes('立即注册'));
                        }
                    """)
                except Exception:
                    pass
                if not is_unregistered_portal:
                    _emit(cb, "  ✅ 学术认证已通过，无需重复填写")
                    return True

            # 检测是否有表单字段出现
            tgt, fn_loc = await _find_visible_locator(page, _FN_SELECTORS, timeout_ms=100)
            if tgt and fn_loc:
                active_target = tgt
                form_found = True
                _emit(cb, f"  ✅ 表单已加载（{wait_s+1}s）")
                break

            # 若尚未出现输入框，检查是否有前置触发按钮（如「Verify eligibility」/「Start verification」）
            if wait_s % 3 == 0:
                t_tgt, trig_btn = await _find_visible_locator(page, _TRIGGER_BUTTON_SELECTORS, timeout_ms=80)
                if t_tgt and trig_btn:
                    try:
                        _emit(cb, "  👉 点击前置认证触发按钮...")
                        await human_hover_and_click(page, trig_btn, timeout=2000)
                        await asyncio.sleep(1.0)
                    except Exception:
                        pass

            if wait_s == 10:
                _emit(
                    cb,
                    f"  ⏳ SheerID 表单仍在加载（{wait_s}s/{form_wait_seconds}s），继续等待，不中途刷新...",
                )

            await asyncio.sleep(1.0)

        if not form_found:
            # 不能继续执行“填写”：没有表单时后续 locator 只会制造假进度。
            try:
                title = await page.title()
            except Exception:
                title = ""
            frame_count = len(page.frames)
            _emit(
                cb,
                f"  ⚠️ {form_wait_seconds} 秒内未挂载 SheerID 表单 "
                f"(title={title[:50] or '-'}, frames={frame_count}, url={page.url[:80]})",
            )
            for entry in diagnostics:
                _emit(cb, f"  🔬 表单加载诊断: {entry}")
            if not diagnostics:
                _emit(cb, "  🔬 未捕获 HTTP/连接失败；仍可能是字段变化、脚本异常或请求尚未完成")

            ss_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "screenshots",
            )
            os.makedirs(ss_dir, exist_ok=True)
            try:
                await page.screenshot(
                    path=os.path.join(ss_dir, f"sheerid_form_missing_{uuid.uuid4().hex}_attempt_{attempt}.png"),
                    full_page=True,
                )
            except Exception:
                pass

            if attempt < max_attempts:
                # 刷新只放在一次完整等待失败之后，并加少量抖动，
                # 避免多个并发账号同时重载、再次抢占网络资源。
                delay = random.uniform(2.0, 5.0)
                _emit(
                    cb,
                    f"  🔄 表单未挂载，{delay:.1f}s 后受控刷新再试 "
                    f"({attempt + 1}/{max_attempts})...",
                )
                await asyncio.sleep(delay)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    _emit(cb, f"  ⚠️ 受控刷新异常: {str(e)[:100]}")
                await asyncio.sleep(2.0)
                continue

            raise SheerIDVerificationError(
                "SheerID 表单连续两次未挂载，已停止假填写并转交全新浏览器重试"
            )

        await asyncio.sleep(0.3)

        # 随机姓名与生日
        first_name = random.choice(_FIRST_NAMES)
        last_name  = random.choice(_LAST_NAMES)
        yr  = random.choice([1997, 1998, 1999, 2000, 2001, 2002])
        mo  = random.randint(1, 12)
        dy  = random.randint(1, 28)
        dob_val = f"{yr}-{mo:02d}-{dy:02d}"

        _emit(cb, f"  👤 填写: {first_name} {last_name} / DOB: {dob_val}")

        targets = [page] + list(page.frames)

        try:
            # ── First name ────────────────────────────────────────────
            tgt_fn, fn_loc = await _find_visible_locator(page, _FN_SELECTORS, timeout_ms=3000)
            if not fn_loc:
                for _w in range(10):
                    tgt_fn, fn_loc = await _find_visible_locator(page, _FN_SELECTORS, timeout_ms=1000)
                    if fn_loc:
                        break
                    await asyncio.sleep(0.5)
            if fn_loc:
                await human_settle(page, min_s=0.3, max_s=0.8, target_locator=fn_loc)
                await human_type(page, fn_loc, first_name)
                await asyncio.sleep(0.2)

            # ── Last name ─────────────────────────────────────────────
            tgt_ln, ln_loc = await _find_visible_locator(page, _LN_SELECTORS, timeout_ms=2000)
            if not ln_loc:
                for _w in range(8):
                    tgt_ln, ln_loc = await _find_visible_locator(page, _LN_SELECTORS, timeout_ms=1000)
                    if ln_loc:
                        break
                    await asyncio.sleep(0.4)
            if ln_loc:
                await human_type(page, ln_loc, last_name)
                await asyncio.sleep(0.2)

            # ── Country — 默认 United States ──────────────────────────
            for t_item in targets:
                try:
                    await t_item.evaluate("""() => {
                        const selects = document.querySelectorAll("#sse-vnext-country-input, select[name*='country' i], select[id*='country' i], select");
                        for (let sel of selects) {
                            if (sel.options && sel.options.length > 0) {
                                let hasValid = sel.value && sel.value !== '' && sel.value !== '0';
                                if (!hasValid) {
                                    for (let opt of sel.options) {
                                        if (opt.value === 'US' || opt.value === 'USA' || (opt.text || '').includes('United States') || (opt.text || '').includes('美国')) {
                                            sel.value = opt.value;
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                    }""")
                except Exception:
                    pass
            await asyncio.sleep(0.1)

            # ── Date of birth — 真实物理输入 ──────────────────────────
            tgt_dob, dob_loc = await _find_visible_locator(page, _DOB_SELECTORS, timeout_ms=2000)
            if dob_loc and await dob_loc.is_visible(timeout=1000):
                await dob_loc.scroll_into_view_if_needed()
                await human_hover_and_click(page, dob_loc, timeout=3000)
                await asyncio.sleep(random.uniform(0.1, 0.2))
                try:
                    await dob_loc.fill(dob_val)
                except Exception:
                    await dob_loc.press_sequentially(dob_val, delay=35)
            await asyncio.sleep(0.2)

            # ── School email — 检查是否已存在，若空则输入 ──────────────
            tgt_em, em_loc = await _find_visible_locator(page, _EMAIL_SELECTORS, timeout_ms=1500)
            if em_loc:
                try:
                    current_val = await em_loc.input_value()
                    if not current_val.strip():
                        await human_type(page, em_loc, ms_email)
                except Exception:
                    pass
            await asyncio.sleep(0.15)

            # ── 勾选必要的非风控同意复选框（仅限必要的第一项，避免误勾选营销推广） ──────
            for t_item in targets:
                try:
                    cbs = t_item.locator("input[type='checkbox']")
                    cb_count = await cbs.count()
                    if cb_count > 0:
                        cb_item = cbs.first
                        if await cb_item.is_visible(timeout=100):
                            c_id = (await cb_item.get_attribute("id") or "").lower()
                            c_name = (await cb_item.get_attribute("name") or "").lower()
                            if not ("cf-" in c_id or "turnstile" in c_id or "cf-" in c_name):
                                is_checked = await cb_item.is_checked()
                                if not is_checked:
                                    b_box = await cb_item.bounding_box()
                                    if b_box and b_box["width"] > 0:
                                        await human_hover_and_click(page, cb_item, timeout=1500)
                except Exception:
                    pass

        except Exception as e:
            _emit(cb, f"  ⚠️ 表单填写异常: {e}")

        # ── 滚动到底部确保 Turnstile 容器进入视口（触发 IntersectionObserver 渲染人机验证） ────
        try:
            tgt_sub, submit_loc = await _find_visible_locator(page, _SUBMIT_SELECTORS, timeout_ms=1000)
            if submit_loc:
                await submit_loc.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)
        except Exception:
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass

        # ── 等待 Verify 按钮激活变蓝（Turnstile 通过） ────
        button_ready = False
        for _pb in range(40):
            is_captcha, cap_msg = await check_captcha_present(page)
            if is_captcha:
                raise AzureCaptchaError(f"触发 Azure 人机拼图验证 ({cap_msg})")

            # 穿透主页面与所有子 iframe，检测并使用原生 CDP 鼠标点击 Cloudflare Turnstile 验证复选框与 Reload
            try:
                targets = [page] + list(page.frames)
                for tgt in targets:
                    tgt_url = (tgt.url or "").lower()
                    is_ts_frame = any(k in tgt_url for k in ("challenges.cloudflare.com", "cloudflare", "turnstile"))

                    # 1. 优先检测并使用真实鼠标点击 Reload Challenge
                    reloaded = False
                    for rel_sel in [
                        "button:has-text('Reload Challenge')",
                        "button:has-text('Reload challenge')",
                        "a:has-text('Reload Challenge')",
                        "a:has-text('Reload challenge')",
                        "button:has-text('Reload')",
                        "a:has-text('Reload')",
                        "#reload-button",
                        ".ctp-reload",
                        "[aria-label*='Reload' i]",
                        "[title*='Reload' i]",
                        "text=Reload Challenge",
                        "text=Reload challenge",
                        "text=重新加载",
                    ]:
                        try:
                            reload_btn = tgt.locator(rel_sel).first
                            if await reload_btn.is_visible(timeout=60):
                                ok = await human_hover_and_click(page, reload_btn, timeout=1500)
                                if ok:
                                    _emit(cb, "  🔄 检测到「Reload Challenge」，已使用原生鼠标点击重新加载人机验证...")
                                    await asyncio.sleep(1.8)
                                    reloaded = True
                                    break
                        except Exception:
                            pass

                    if reloaded:
                        continue

                    # 2. 原生受信任鼠标事件点击 Turnstile 交互式验证复选框
                    # 注意：仅在 Turnstile 专用 iframe 中匹配通用 input[type='checkbox']，防止误点击主表单协议复选框
                    ts_selectors = [
                        ".ctp-checkbox-label",
                        ".ctp-checkbox-container",
                        "div.cb-c",
                        "span.ctp-label",
                        "label.ctp-checkbox-label",
                        "#challenge-stage input",
                        "#cf-stage",
                        "div#challenge-stage",
                        ".mark",
                        "[aria-label*='Cloudflare' i]",
                        "[aria-label*='Turnstile' i]",
                    ]
                    if is_ts_frame:
                        ts_selectors.insert(0, "input[type='checkbox']")

                    for ts_sel in ts_selectors:
                        try:
                            ts_box = tgt.locator(ts_sel).first
                            if await ts_box.is_visible(timeout=60):
                                ok = await human_hover_and_click(page, ts_box, timeout=2000)
                                if ok:
                                    _emit(cb, "  🤖 检测到 Cloudflare Turnstile 验证框，已拟人化模拟点击...")
                                    await asyncio.sleep(2.0)
                                    break
                        except Exception:
                            pass
            except Exception:
                pass

            # 自然拟人鼠标微动（触发 Turnstile 与风控探针活跃度评估）
            if _pb % 3 == 0:
                try:
                    vp = page.viewport_size or {"width": 1920, "height": 1080}
                    w, h = vp.get("width", 1920), vp.get("height", 1080)
                    target_x = random.randint(int(w * 0.35), int(w * 0.65))
                    target_y = random.randint(int(h * 0.35), int(h * 0.65))
                    await human_bezier_move(page, target_x, target_y)
                except Exception:
                    pass

            # 周期性检测表单各字段是否就绪，若有缺失使用原生方式补全
            if _pb % 8 == 0 and _pb > 0:
                try:
                    tgt_f, fn_c = await _find_visible_locator(page, _FN_SELECTORS, timeout_ms=50)
                    if fn_c:
                        cur = await fn_c.input_value()
                        if not cur or not cur.strip():
                            await human_type(page, fn_c, first_name)
                    tgt_l, ln_c = await _find_visible_locator(page, _LN_SELECTORS, timeout_ms=50)
                    if ln_c:
                        cur = await ln_c.input_value()
                        if not cur or not cur.strip():
                            await human_type(page, ln_c, last_name)
                except Exception:
                    pass

            # 检查 Verify 按钮是否已激活变蓝（穿透所有 Frame）
            for tgt in targets:
                try:
                    state = await tgt.evaluate(_btn_js)
                    if state == "enabled":
                        button_ready = True
                        break
                except Exception:
                    pass
            if button_ready:
                break

            await asyncio.sleep(1)

        if not button_ready:
            _emit(cb, "  ⚠️ Verify 按钮在 40 秒内未激活变蓝")
            if attempt < max_attempts:
                _emit(cb, f"  ⚠️ 正在自动刷新页面重试 (第 {attempt+1}/{max_attempts} 次)...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            else:
                raise SheerIDVerificationError("SheerID 提交按钮未激活变蓝且单浏览器内已刷新重试 1 次仍未就绪")

        await asyncio.sleep(random.uniform(0.40, 0.80))

        # ── 点击 Next / Verify academic status（严格排除第二步 accept terms 按钮） ──
        _STEP1_CLICK_JS = """() => {
            const btns = [...document.querySelectorAll('button, input[type="submit"]')].filter(b => {
                const r = b.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            // 优先级 1：精确匹配 "Next" / "下一步"
            let btn = btns.find(b => {
                const t = (b.textContent || b.value || '').trim();
                return t === 'Next' || t === '下一步';
            });
            // 优先级 2："Verify academic status" 但排除 "accept terms"
            if (!btn) {
                btn = btns.find(b => {
                    const t = (b.textContent || b.value || '').trim().toLowerCase();
                    if (t.includes('accept terms') || t.includes('接受条款')) return false;
                    return t.includes('verify') || t.includes('验证');
                });
            }
            // 优先级 3：#sse-vnext-submit-button（也排除 accept terms）
            if (!btn) {
                const sse = document.querySelector('#sse-vnext-submit-button');
                if (sse) {
                    const t = (sse.textContent || sse.value || '').trim().toLowerCase();
                    if (!t.includes('accept terms') && !t.includes('接受条款')) btn = sse;
                }
            }
            if (!btn) return { clicked: false, text: '' };
            btn.scrollIntoView({ block: 'center' });
            btn.click();
            return { clicked: true, text: (btn.textContent || btn.value || '').trim() };
        }"""

        clicked_ok = False
        btn_txt = "Next"
        # 方式 1：Playwright Locator（优先 "Next" 精确匹配）
        for sel in ["button:text-is('Next')", "button:text-is('下一步')"]:
            tgt_sub, verify_btn = await _find_visible_locator(page, [sel], timeout_ms=800)
            if verify_btn:
                try:
                    btn_txt = (await verify_btn.inner_text() or "Next").strip()
                except Exception:
                    pass
                try:
                    await verify_btn.scroll_into_view_if_needed()
                    clicked_ok = await human_hover_and_click(page, verify_btn, timeout=5000)
                except Exception:
                    try:
                        await verify_btn.click(timeout=3000)
                        clicked_ok = True
                    except Exception:
                        pass
                if clicked_ok:
                    break

        # 方式 2：Playwright Locator（"Verify academic status" 排除 accept terms）
        if not clicked_ok:
            for sel in _SUBMIT_SELECTORS:
                tgt_sub, verify_btn = await _find_visible_locator(page, [sel], timeout_ms=600)
                if verify_btn:
                    try:
                        txt = (await verify_btn.inner_text() or "").strip().lower()
                        if "accept terms" in txt or "接受条款" in txt:
                            continue
                        btn_txt = (await verify_btn.inner_text() or "Verify").strip()
                    except Exception:
                        pass
                    try:
                        await verify_btn.scroll_into_view_if_needed()
                        clicked_ok = await human_hover_and_click(page, verify_btn, timeout=5000)
                    except Exception:
                        try:
                            await verify_btn.click(timeout=3000)
                            clicked_ok = True
                        except Exception:
                            pass
                    if clicked_ok:
                        break

        # 方式 3：纯 JS 兜底点击（带优先级排序）
        if not clicked_ok:
            for tgt in targets:
                try:
                    res = await tgt.evaluate(_STEP1_CLICK_JS)
                    if res and res.get("clicked"):
                        btn_txt = res.get("text", "Verify")
                        clicked_ok = True
                        break
                except Exception:
                    pass

        is_next_btn = btn_txt.strip().lower() in ("next", "下一步")
        _emit(cb, f"  🎓 点击 {btn_txt}")

        is_captcha, cap_msg = await check_captcha_present(page)
        if is_captcha:
            raise AzureCaptchaError(f"触发 Azure 人机拼图验证 ({cap_msg})")

        # ── 自适应检测与处理第二步条款确认（适配部分账号出现的 Terms 卡片） ──
        # 点击 "Next" 后 Terms 面板必定出现，等待更长；旧流程 "Verify academic status" 后短探测即可
        _terms_wait = 15.0 if is_next_btn else 8.0
        terms_handled = await _handle_terms_acceptance_if_present(page, cb, max_wait_s=_terms_wait)
        if terms_handled:
            _emit(cb, "  ✅ 条款确认与验证完成，进入资格审核与跳转确认...")
            if ms_email:
                try:
                    from modules.key_extractor import mark_terms_accepted
                    mark_terms_accepted(ms_email)
                except Exception:
                    pass

        _DONE_JS = """() => {
            const t = (document.body ? document.body.innerText : '').toLowerCase();
            const u = (window.location.href || '').toLowerCase();
            return u.includes('portal.azure.com') ||
                   u.includes('education.azure.com') ||
                   u.includes('signup?offer=') ||
                   t.includes('confirming your account') ||
                   t.includes('正在确认') ||
                   t.includes('verification email has been sent');
        }"""

        try:
            await page.wait_for_function(_DONE_JS, timeout=12000)
            _emit(cb, "  ✅ 表单已提交")
        except Exception:
            # 容错：即使未能精确匹配 _DONE_JS，只要已点击且未报错，直接转入结果轮询
            pass

        # ── 等待确认（20 轮 × 3 秒 = 60 秒） ─────────────────
        _emit(cb, "  ⏳ 正在等待 SheerID 审核与资格确认...")
        verification_passed = False

        for i in range(20):
            is_captcha, cap_msg = await check_captcha_present(page)
            if is_captcha:
                raise AzureCaptchaError(f"触发 Azure 人机拼图验证 ({cap_msg})")

            await asyncio.sleep(3)
            url = (page.url or "").lower()
            try:
                body = await page.locator("body").inner_text(timeout=3000)
                body_lower = body.lower()
            except Exception:
                body_lower = ""

            # 0. 明确拒绝/审核失败/要求上传文档/封控拦截
            if any(k in body_lower for k in (
                "unable to verify", "could not verify", "not eligible", "unfortunately",
                "upload documentation", "upload a document", "please upload", "upload proof",
                "document required", "maximum verification attempts", "we need more information",
                "无法验证", "不符合资格", "请上传", "上传文档", "上传学生证明", "已达到最大验证尝试次数"
            )):
                _emit(cb, "  ❌ SheerID 学术审核明确被拒 / 需要人工上传证明文件")
                raise SheerIDVerificationError("SheerID 审核未通过: 账号被拒或要求人工上传学生证件")

            # 0b. 兼容慢网延迟：若条款卡片在此处才延迟渲染出来，立即自适应处理
            if any(k in body_lower for k in ("subscription agreement", "订阅协议", "accept terms", "接受条款", "dev tools")):
                th = await _handle_terms_acceptance_if_present(page, cb, max_wait_s=2.5)
                if th:
                    _emit(cb, "  ✅ [延迟捕获] 条款确认与验证完成")
                    if ms_email:
                        try:
                            from modules.key_extractor import mark_terms_accepted
                            mark_terms_accepted(ms_email)
                        except Exception:
                            pass
                    continue

            # 1. 成功跳转到 Portal / 业务平台
            if "portal.azure.com" in url or "education.azure.com" in url:
                _emit(cb, f"  ✅ Academic Verification 确认完成（页面已跳转: {url[:60]}）")
                verification_passed = True
                return True

            # 1b. 跳转到 signup.azure.com/signup — 需要完成订阅激活
            if "signup.azure.com/signup" in url and "/studentverification" not in url:
                _emit(cb, f"  ✅ SheerID 学术认证通过，进入订阅激活阶段: {url[:60]}")
                result = await _complete_azure_offer_signup(page, ms_email, cb)
                if result:
                    verification_passed = True
                    return True
                continue

            # 2. 正在审核中/过渡状态，持续等待
            if "confirming your account" in body_lower or "setting up" in body_lower or "verifying" in body_lower or "正在确认" in body_lower:
                _emit(cb, f"  ⏳ 正在确认账户 (confirming your account, {i+1}/20)...")
                continue

            # 3. 成功通过标志（到达 Portal / 验证完成 / 邮件已发送）
            if any(k in body_lower for k in (
                "verification complete", "verification email has been sent", "已发送验证电子邮件",
                "you are verified", "you're verified", "you've been verified",
                "academic verification approved", "academic status confirmed", "verification successful",
                "congratulations", "已完成验证", "验证完成", "学术验证通过", "已确认你的学生状态", "恭喜"
            )):
                _emit(cb, "  ✅ Academic Verification 确认完成")
                verification_passed = True
                return True

            # 3b. 停留在 signup.azure.com 且页面有 profile/offer 内容 — 需要完成订阅激活
            if "signup.azure.com" in url and "/studentverification" not in url and any(k in body_lower for k in (
                "your profile", "你的个人资料", "面向学生的 azure", "azure for students",
                "sign up", "注册", "subscription agreement",
            )):
                _emit(cb, "  ✅ SheerID 认证通过，检测到订阅激活页面，开始填写...")
                result = await _complete_azure_offer_signup(page, ms_email, cb)
                if result:
                    verification_passed = True
                    return True
                continue

        # 若本次尝试在 60 秒内均未确认成功，触发刷新重试
        if not verification_passed:
            if attempt < max_attempts:
                _emit(cb, f"  ⚠️ SheerID 提交后超时未确认成功，正在自动刷新页面重试 (第 {attempt+1}/{max_attempts} 次)...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            else:
                raise SheerIDVerificationError("SheerID 学术核验单浏览器内刷新重试后仍未确认成功")

    return True
