"""
高拟真真人交互动力学引擎 (Human Interaction Dynamics Engine v2)
--------------------------------------------------------------
核心特性：
1. 连续物理鼠标坐标跟踪（Cursor Continuity）：基于会话上下文持续跟踪鼠标物理坐标，彻底消除「跨越式瞬移 (Teleportation)」特征；
2. 三次贝塞尔曲线与费茨定律（Fitts's Law）：模拟手部肌肉微扰、亚像素手抖（Hand Tremor）与终点减速对齐；
3. 真实键盘击键动力学（Keystroke Dynamics）：非匀速击键，包含人类认知停顿、特殊符号迟滞（如 '@', '.', 数字）与输入完成复查停顿；
4. 100% 原生 CDP 硬件级事件：严禁派发任何 isTrusted === false 的合成 DOM 事件。
"""

import asyncio
import logging
import math
import random
from typing import Optional, Tuple, Union
from playwright.async_api import Locator, Page

log = logging.getLogger(__name__)

# 物理光标坐标池：记录每个 Page 对象的当前真实指针位置
_PAGE_CURSOR_COORDS: dict[int, Tuple[float, float]] = {}


def get_cursor_position(page: Page) -> Tuple[float, float]:
    """获取当前页面物理光标的已知坐标。若首次调用，从自然视口边缘生成。"""
    page_id = id(page)
    if page_id in _PAGE_CURSOR_COORDS:
        return _PAGE_CURSOR_COORDS[page_id]
    vp = page.viewport_size or {"width": 1920, "height": 1080}
    w, h = vp.get("width", 1920), vp.get("height", 1080)
    # 真实用户初次接入时鼠标通常位于视口左上方或顶部边缘
    init_x = random.uniform(w * 0.1, w * 0.3)
    init_y = random.uniform(h * 0.08, h * 0.25)
    _PAGE_CURSOR_COORDS[page_id] = (init_x, init_y)
    return init_x, init_y


def set_cursor_position(page: Page, x: float, y: float):
    """更新当前页面的物理光标记录。"""
    _PAGE_CURSOR_COORDS[id(page)] = (x, y)


async def human_bezier_move(
    page: Page,
    target_x: float,
    target_y: float,
    steps: int = 0,
) -> None:
    """
    使用三次贝塞尔曲线 + 动力学微扰模拟真实手部鼠标位移。
    严格从上一时刻的实际位置出发，平滑移动至目标点。
    """
    page_id = id(page)
    vp = page.viewport_size or {"width": 1920, "height": 1080}
    w, h = vp.get("width", 1920), vp.get("height", 1080)

    curr_x, curr_y = get_cursor_position(page)
    dist = math.hypot(target_x - curr_x, target_y - curr_y)
    if dist < 3.0:
        set_cursor_position(page, target_x, target_y)
        return

    # 依据位移欧式距离动态匹配步数（短距离 10~14 步，长距离 18~28 步）
    if steps <= 0:
        steps = max(10, min(28, int(dist / 22.0) + random.randint(4, 8)))

    # 控制点生成（符合人体工程学的手腕弧度）
    angle = math.atan2(target_y - curr_y, target_x - curr_x)
    normal_angle = angle + math.pi / 2

    # 随机偏角曲率（向左或向右微弧）
    curve_direction = 1 if random.random() < 0.5 else -1
    dev1 = curve_direction * random.uniform(15.0, min(80.0, dist * 0.35))
    dev2 = curve_direction * random.uniform(10.0, min(50.0, dist * 0.20))

    ctrl1_x = curr_x + (target_x - curr_x) * random.uniform(0.20, 0.40) + math.cos(normal_angle) * dev1
    ctrl1_y = curr_y + (target_y - curr_y) * random.uniform(0.20, 0.40) + math.sin(normal_angle) * dev1
    ctrl2_x = curr_x + (target_x - curr_x) * random.uniform(0.60, 0.80) + math.cos(normal_angle) * dev2
    ctrl2_y = curr_y + (target_y - curr_y) * random.uniform(0.60, 0.80) + math.sin(normal_angle) * dev2

    for i in range(1, steps + 1):
        t = i / steps
        # 费茨定律：S 形缓动 (Ease-in-out Smoothstep)
        t_eased = t * t * (3.0 - 2.0 * t)

        bx = (1 - t_eased)**3 * curr_x + 3 * (1 - t_eased)**2 * t_eased * ctrl1_x + 3 * (1 - t_eased) * t_eased**2 * ctrl2_x + t_eased**3 * target_x
        by = (1 - t_eased)**3 * curr_y + 3 * (1 - t_eased)**2 * t_eased * ctrl1_y + 3 * (1 - t_eased) * t_eased**2 * ctrl2_y + t_eased**3 * target_y

        # 自然手抖动微扰（0.3 像素以内，步数末尾收敛归零）
        decay = (1.0 - t)
        jitter_x = random.uniform(-0.4, 0.4) * decay
        jitter_y = random.uniform(-0.4, 0.4) * decay

        step_x = max(0.0, min(float(w), bx + jitter_x))
        step_y = max(0.0, min(float(h), by + jitter_y))

        try:
            await page.mouse.move(step_x, step_y)
            set_cursor_position(page, step_x, step_y)
        except Exception:
            pass

        # 微步间隔：8~16ms
        await asyncio.sleep(random.uniform(0.007, 0.016))

    set_cursor_position(page, target_x, target_y)


async def human_settle(
    page: Page,
    min_s: float = 1.2,
    max_s: float = 2.4,
    target_locator: Optional[Locator] = None
) -> None:
    """
    等待页面与后台安全遥测 (Arkose Labs / Cloudflare Turnstile / FPT / ASM) 静默初始化，
    并伴随拟人化视线微扫描（微幅移动光标），彻底消除快速动作（Fast-Action）特征。
    """
    settle_time = random.uniform(min_s, max_s)
    vp = page.viewport_size or {"width": 1920, "height": 1080}
    w, h = vp.get("width", 1920), vp.get("height", 1080)

    # 视线微扫描位移
    drift_x = random.uniform(w * 0.35, w * 0.65)
    drift_y = random.uniform(h * 0.25, h * 0.65)
    await human_bezier_move(page, drift_x, drift_y, steps=random.randint(8, 14))

    await asyncio.sleep(settle_time * 0.45)

    if target_locator:
        try:
            await target_locator.scroll_into_view_if_needed()
            box = await target_locator.bounding_box()
            if box:
                tx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
                ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
                await human_bezier_move(page, tx, ty, steps=random.randint(10, 16))
        except Exception:
            pass

    await asyncio.sleep(settle_time * 0.55)


async def human_hover_and_click(
    page: Page,
    locator_or_selector: Union[Locator, str],
    timeout: int = 5000,
    pre_hover: Tuple[float, float] = (0.15, 0.32),
    post_click: Tuple[float, float] = (0.25, 0.50),
    force: bool = False,
) -> bool:
    """
    拟人化鼠标移动、悬停并执行硬件级 CDP 点击。
    """
    try:
        if isinstance(locator_or_selector, str):
            loc = page.locator(locator_or_selector).first
        else:
            loc = locator_or_selector

        await loc.scroll_into_view_if_needed()
        box = await loc.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return False

        # 随机击中元素安全区（偏中心位置）
        target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)

        # 1. 连续曲线移动到目标
        await human_bezier_move(page, target_x, target_y)

        # 2. 真人悬停视线确认延迟
        await asyncio.sleep(random.uniform(*pre_hover))

        # 3. 原生 CDP 点击
        try:
            await page.mouse.click(target_x, target_y)
        except Exception:
            try:
                await loc.click(timeout=timeout, force=force)
            except Exception:
                pass

        # 4. 点击后操作延迟
        await asyncio.sleep(random.uniform(*post_click))
        return True
    except Exception as e:
        log.debug(f"human_hover_and_click 异常: {e}")
        return False


async def human_type(
    page: Page,
    locator_or_selector: Union[Locator, str],
    text: str,
    min_delay: float = 0.05,
    max_delay: float = 0.12,
) -> bool:
    """
    拟人化物理键盘键入：
    1. 平滑移动光标聚焦；
    2. 清空既有内容（Control+A + Backspace）；
    3. 逐字物理击键，在特殊符号/数字处伴随真人认知停顿；
    4. 敲击完毕后进行短暂停顿（模拟核对文本）。
    """
    try:
        if isinstance(locator_or_selector, str):
            loc = page.locator(locator_or_selector).first
        else:
            loc = locator_or_selector

        try:
            await loc.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        box = None
        try:
            box = await loc.bounding_box(timeout=2000)
        except Exception:
            pass
        if box and box["width"] > 0 and box["height"] > 0:
            tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await human_bezier_move(page, tx, ty)
            await asyncio.sleep(random.uniform(0.08, 0.15))

        await loc.click(timeout=2500)
        await asyncio.sleep(random.uniform(0.18, 0.35))

        # 清空已有文本
        try:
            curr = await loc.input_value()
            if curr:
                await page.keyboard.press("Control+A")
                await asyncio.sleep(random.uniform(0.04, 0.08))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.06, 0.12))
        except Exception:
            pass

        # 逐字拟人击键
        for idx, char in enumerate(text):
            kd = random.uniform(min_delay, max_delay)
            # 在邮箱符号、域名分割点或数字处人类有轻微停顿
            if char in ("@", ".", "-", "_") or char.isdigit():
                kd += random.uniform(0.08, 0.18)
            elif random.random() < 0.06 and idx > 2:
                kd += random.uniform(0.15, 0.30)

            await page.keyboard.type(char, delay=int(random.uniform(25, 60)))
            await asyncio.sleep(kd)

        # 校验填入结果
        try:
            val = await loc.input_value()
            if not val and text:
                await loc.fill(text)
        except Exception:
            pass

        # 完成输入后的核对与视线停顿 (0.35~0.75s)
        await asyncio.sleep(random.uniform(0.35, 0.75))
        return True
    except Exception as e:
        log.debug(f"human_type 异常: {e}")
        return False
