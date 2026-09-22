"""
人机验证 (CAPTCHA / Arkose Labs Puzzle / Cloudflare Turnstile / SheerID Challenge) 精确检测模块
-----------------------------------------------------------------------------------------
穿透扫描页面及所有嵌入的子框架 (iframe)，结合可见性与组件特征精准识别各类交互式拼图与人机挑战。
严格杜绝把后台静默风控探针或常规“学术验证”表单误判为人机拼图。
"""

import logging
from typing import Tuple
from playwright.async_api import Page

log = logging.getLogger(__name__)

# 仅匹配具有明确「解题/拼图/机器人证明」语义的人机交互短语
CAPTCHA_SIGNATURE_PHRASES = (
    "please solve the puzzle so we know you're not a robot",
    "please solve the puzzle",
    "solve the puzzle",
    "prove you're human",
    "prove you are human",
    "verify you are human",
    "verify you're human",
    "verify that you are human",
    "security challenge",
    "press and hold",
    "请解答难题以证明您不是机器人",
    "请解答难题",
    "解答难题",
    "证明您是真人",
    "验证您是真人",
    "人机身份验证",
    "进行人机身份验证",
)

CAPTCHA_IFRAME_PATTERNS = (
    "arkoselabs",
    "arkose",
    "funcaptcha",
    "enforcement",
    "turnstile",
    "challenges.cloudflare.com",
    "recaptcha",
    "hcaptcha",
)


# 真实交互式人机拼图/交互挑战的专用选择器（仅匹配真实渲染的画板或互动元素，严格排除静态 iframe 与可自动点击的 Turnstile 复选框）
CAPTCHA_ACTIVE_PUZZLE_SELECTORS = [
    "canvas.game-canvas",
    "canvas#game-canvas",
    "#game_children_challenge canvas",
    ".challenge-instructions",
    ".prompt-text",
    ".geetest_radar_tip",
]


async def check_captcha_present(page: Page) -> Tuple[bool, str]:
    """
    穿透检测主页面及所有子框架，结合尺寸与可见性寻找真实弹出的人机拼图/挑战。
    返回: (True/False, 匹配到的特征描述)
    严格放行仅用于后台静默风控信誉评估的探针 iframe。
    """
    try:
        frames = page.frames
        for frame in frames:
            try:
                url_lower = (frame.url or "").lower()

                # 1. 检查主页面文本中是否包含明确的人机验证提示
                if frame == page.main_frame:
                    body_txt = await frame.evaluate("() => document.body ? document.body.innerText : ''")
                    body_lower = (body_txt or "").lower()
                    for phrase in CAPTCHA_SIGNATURE_PHRASES:
                        if phrase in body_lower:
                            # 确认页面上有可见的交互或提示区域
                            is_vis = await page.evaluate("""() => {
                                const els = [...document.querySelectorAll('iframe, div, section, p, span, h1, h2, h3')];
                                return els.some(el => {
                                    const r = el.getBoundingClientRect();
                                    const s = window.getComputedStyle(el);
                                    if (r.width < 50 || r.height < 20 || s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') {
                                        return false;
                                    }
                                    const t = (el.innerText || '').toLowerCase();
                                    return t.includes('puzzle') || t.includes('难题') || t.includes('not a robot') || t.includes('不是机器人');
                                });
                            }""")
                            if is_vis:
                                return True, f"主页面人机提示: '{phrase}'"
                    continue

                # 2. 检查子 iframe（如 Arkose Labs / Turnstile / SheerID Challenge）
                try:
                    frame_el = await frame.frame_element()
                    if not frame_el:
                        continue

                    box = await frame_el.bounding_box()
                    if not box or box["width"] < 80 or box["height"] < 60:
                        # 0尺寸或极小后台探针（如静默评估信誉 iframe），放行
                        continue

                    is_visible = await frame_el.is_visible()
                    if not is_visible:
                        continue
                except Exception:
                    continue

                # 3. 检查 iframe 内部文本中是否包含需要解答拼图的明确提示 (排除可自动点击的 Cloudflare Turnstile)
                is_turnstile_frame = any(k in url_lower for k in ("challenges.cloudflare.com", "cloudflare", "turnstile"))
                if not is_turnstile_frame:
                    body_txt = await frame.evaluate("() => document.body ? document.body.innerText : ''")
                    body_lower = (body_txt or "").lower()

                    for phrase in CAPTCHA_SIGNATURE_PHRASES:
                        if phrase in body_lower:
                            return True, f"可见人机拼图短语: '{phrase}' ({int(box['width'])}x{int(box['height'])})"

                # 4. 检查 iframe 内部是否存在真实激活的拼图 Canvas / 游戏核心
                has_active_puzzle = await frame.evaluate("""(selectors) => {
                    return selectors.some(sel => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width >= 40 && r.height >= 40 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                    });
                }""", CAPTCHA_ACTIVE_PUZZLE_SELECTORS)
                if has_active_puzzle:
                    return True, f"检测到真实交互式 CAPTCHA 拼图组件 ({int(box['width'])}x{int(box['height'])})"

            except Exception:
                continue

    except Exception as e:
        log.debug(f"检查 CAPTCHA 异常: {e}")

    return False, ""

