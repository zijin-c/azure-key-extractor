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
  2. 等待 SheerID 表单渲染（#sse-vnext-fname-input 出现，最多 60s）
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
    "#sse-vnext-submit-button",
    "button:has-text('Verify academic status')", "button:has-text('验证学术状态')",
    "button:has-text('Verify status')", "button:has-text('Verify')",
    "button:has-text('验证')", "button[type='submit']", "input[type='submit']"
]

_TRIGGER_BUTTON_SELECTORS = [
    "button:has-text('Verify academic status')", "button:has-text('Verify eligibility')",
    "button:has-text('Start verification')", "button:has-text('Check eligibility')",
    "button:has-text('验证学术状态')", "button:has-text('开始验证')",
    "button:has-text('Get started')", "button:has-text('开始使用')",
    "button:has-text('Next')", "button:has-text('下一步')", "button:has-text('Continue')"
]

_btn_js = """() => {
    const btn = document.querySelector("#sse-vnext-submit-button") ||
                [...document.querySelectorAll("button, input[type='submit']")].find(x => {
                    const txt = (x.textContent || x.value || '').trim();
                    return txt.includes("Verify") || txt.includes("验证") || txt.includes("Submit");
                });
    if (!btn) return "not_found";
    const r = btn.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return "hidden";
    if (btn.disabled) return "disabled";
    if (btn.getAttribute("aria-disabled") === "true") return "disabled";
    const style = window.getComputedStyle(btn);
    if (style.pointerEvents === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") < 0.6) return "disabled";
    return "enabled";
}"""


def _emit(cb: ProgressCallback, msg: str):
    log.info(msg)
    if cb:
        cb(msg)


async def _find_visible_locator(page: Page, selectors: list[str], timeout_ms: int = 150):
    """穿透页面及所有子 iframe，查找第一个可见的目标元素。"""
    targets = [page] + list(page.frames)
    sel_query = ", ".join(selectors)
    for tgt in targets:
        try:
            loc = tgt.locator(sel_query).first
            if await loc.is_visible(timeout=timeout_ms):
                return tgt, loc
        except Exception:
            pass
    return None, None


async def fill_azure_profile_form(page: Page, ms_email: str, cb: ProgressCallback = None) -> bool:
    """
    填写 Academic Verification 表单并提交，支持单浏览器内 1 次刷新重试机制 (max_attempts = 2)。
    必须确凿确认认证成功后才返回 True；若尝试均超时则抛出 SheerIDVerificationError 触发新开浏览器重试。
    """
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        _emit(cb, f"  📝 执行 SheerID 学术资格认证 (尝试 {attempt}/{max_attempts})...")

        # 1. 快速检查是否已在 Portal / 后续成功页面
        cur_url = (page.url or "").lower()
        if "portal.azure.com" in cur_url or "education.azure.com" in cur_url or "signup?offer=" in cur_url:
            _emit(cb, "  ✅ 学术认证已通过，无需重复填写")
            return True

        # 2. 等待 SheerID 表单渲染（最多 30s，支持点击触发按钮与超时主动重载恢复）
        _emit(cb, "  📝 等待 Academic Verification 表单加载...")
        form_found = False
        active_target = page

        for wait_s in range(30):
            cur_url = (page.url or "").lower()
            if "portal.azure.com" in cur_url or "education.azure.com" in cur_url or "signup?offer=" in cur_url:
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

            # 若 15 秒页面仍然空白/无任何表单节点，主动刷新页面促使 Angular 重新拉取
            if wait_s == 15 and not form_found:
                _emit(cb, "  ⚠️ 页面加载迟缓，主动重新加载学生认证页面...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass

            await asyncio.sleep(1.0)

        if not form_found:
            _emit(cb, "  ⚠️ 未等到表单就绪，尝试全 DOM 穿透探测...")

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

            # ── 勾选必要的非风控同意复选框 ────────────────────────────
            for t_item in targets:
                try:
                    cbs = t_item.locator("input[type='checkbox']")
                    cb_count = min(await cbs.count(), 5)
                    for c_i in range(cb_count):
                        cb_item = cbs.nth(c_i)
                        if await cb_item.is_visible(timeout=100):
                            c_id = (await cb_item.get_attribute("id") or "").lower()
                            c_name = (await cb_item.get_attribute("name") or "").lower()
                            if "cf-" in c_id or "turnstile" in c_id or "cf-" in c_name:
                                continue
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
        for _pb in range(90):
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
            _emit(cb, "  ⚠️ Verify 按钮在 90 秒内未激活变蓝")
            if attempt < max_attempts:
                _emit(cb, f"  ⚠️ 正在自动刷新页面重试 (第 {attempt+1}/{max_attempts} 次)...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            else:
                raise SheerIDVerificationError("SheerID 提交按钮未激活变蓝且单浏览器内已刷新重试 1 次仍未就绪")

        await asyncio.sleep(random.uniform(0.40, 0.80))

        # ── 点击 Verify academic status ───────────────────────────────
        tgt_sub, verify_btn = await _find_visible_locator(page, _SUBMIT_SELECTORS, timeout_ms=2000)
        if not verify_btn:
            verify_btn = page.locator(", ".join(_SUBMIT_SELECTORS)).first

        await verify_btn.scroll_into_view_if_needed()
        await human_hover_and_click(page, verify_btn, timeout=8000)

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

        submitted = False
        is_captcha, cap_msg = await check_captcha_present(page)
        if is_captcha:
            raise AzureCaptchaError(f"触发 Azure 人机拼图验证 ({cap_msg})")

        _emit(cb, "  🎓 点击 Verify academic status")
        try:
            await verify_btn.click(timeout=8000)
        except Exception:
            for tgt in targets:
                try:
                    await tgt.evaluate(
                        "() => { const b = document.querySelector('#sse-vnext-submit-button') || [...document.querySelectorAll('button, input[type=\"submit\"]')].find(x => (x.textContent || x.value || '').includes('Verify') || (x.textContent || x.value || '').includes('验证')); if(b) b.click(); }"
                    )
                except Exception:
                    pass

        try:
            await page.wait_for_function(_DONE_JS, timeout=25000)
            submitted = True
            _emit(cb, "  ✅ 表单已提交")
        except Exception:
            # 容错：即使未能精确匹配 _DONE_JS，只要已点击且未报错，直接转入结果轮询
            submitted = True
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

            # 1. 成功跳转到 Portal / 业务平台 / 注册 Offer 页面
            if "portal.azure.com" in url or "education.azure.com" in url or "signup?offer=" in url:
                _emit(cb, f"  ✅ Academic Verification 确认完成（页面已跳转: {url[:60]}）")
                verification_passed = True
                return True

            # 2. 正在审核中/过渡状态，持续等待
            if "confirming your account" in body_lower or "setting up" in body_lower:
                _emit(cb, f"  ⏳ 正在确认账户 (confirming your account, {i+1}/20)...")
                continue

            # 3. 成功通过标志（到达 Your profile / 个人资料 / 验证完成 / 邮件已发送）
            if any(k in body_lower for k in (
                "your profile", "你的个人资料", "first name", "面向学生的 azure", "azure for students",
                "verification complete", "verification email has been sent", "已发送验证电子邮件",
                "you are verified", "you're verified", "you've been verified",
                "academic verification approved", "academic status confirmed", "verification successful",
                "congratulations", "已完成验证", "验证完成", "学术验证通过", "已确认你的学生状态", "恭喜"
            )):
                _emit(cb, "  ✅ Academic Verification 确认完成")
                verification_passed = True
                return True

        # 若本次尝试在 60 秒内均未确认成功，触发刷新重试
        if not verification_passed:
            if attempt < max_attempts:
                _emit(cb, f"  ⚠️ SheerID 提交后超时未确认成功，正在自动刷新页面重试 (第 {attempt+1}/{max_attempts} 次)...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            else:
                raise SheerIDVerificationError("SheerID 学术核验单浏览器内刷新重试后仍未确认成功")

    return True
