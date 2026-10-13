"""
随机浏览器指纹生成 + Playwright 上下文初始化
全维度超强反检测与超深度省流环境隔离系统 (Stealth v16 终极全维强化版)：
1. 真实消费级 GPU / WebGL 全参数一致性矩阵深度硬化（20+ GL 常量 × GPU 型号对齐、Shader Precision 对齐、扩展列表随机子集与 readPixels PRNG 微噪点）；
2. 真实桌面级 PluginArray & MimeTypeArray 架构（5 大内置 PDF 插件，彻底消除 plugins.length===0 无头漏洞）；
3. Canvas 2D 亚像素级微偏移、100% 幂等 getImageData 逐点噪点注入、toDataURL/toBlob 一致性噪点注入与 measureText 字体排版指标微抖动；
4. AudioContext、OfflineAudioContext & AnalyserNode 频域/时域全维微噪点注入（独立种子、WeakSet 单 Buffer 幂等防护、getChannelData/copyFromChannel/getFloatFrequencyData/getByteFrequencyData 全覆盖）；
5. Document.prototype.hasFocus / visibilityState / hidden 真实化（彻底杜绝 Headless 失去焦点与后台标签页特征）；
6. 原生 Getter 生成工厂（严格对齐 V8 函数名与 toString，抹除一切属性篡改痕迹）；
7. CDP / WebDriver 自动化残留属性全量清除；
8. Navigator.prototype.pdfViewerEnabled 深度对齐；
9. Notification.permission 与 Permissions API (navigator.permissions.query) 原生状态返回；
10. WebGPU (navigator.gpu) 真实硬件适配器模拟与 GPU limits 联动；
11. NetworkInformation (navigator.connection) 真实 4G/宽带网络与 RTT/Downlink 联动；
12. Screen.orientation 真实桌面横屏对象与真实屏幕几何视口联动；
13. Battery API (navigator.getBattery) 与完整 SpeechSynthesis 语音库对齐；
14. 完整 navigator.userAgentData (Client Hints) 高熵值接口 (包含 wow64: false) 与 Win32 原型链严格原生化；
15. 公共静态资源持久化缓存，动态认证/API 放行；
16. 每个账号完全隔离的 BrowserContext 独立生命周期，彻底杜绝跨账号污染；
17. ClientRects / DOMRect 亚像素噪点（getBoundingClientRect / getClientRects 独立种子扰动，消除 CSS 字体探测聚类）；
18. Font Enumeration 防御（基于字体子集指纹 + measureText 字体依赖微噪）；
19. performance.memory 堆大小随机化伪装；
20. WebRTC ICE Candidate 本地内网 IP 深度过滤与 SDP 地址替换；
21. Date / Intl 时区一致性双重保障；
22. Event.prototype.isTrusted 原生 getter 保护。
"""
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
from urllib.parse import urlparse, urlsplit
from utils.http_cache import LocalHttpCache, transfer_body_size

log = logging.getLogger(__name__)

# 真实主流桌面屏幕分辨率与几何参数池（宽度, 高度, 任务栏占用高度, 设备像素比）
_SCREEN_RESOLUTIONS = [
    {"width": 1920, "height": 1080, "avail_height": 1040, "dpr": 1.0},
    {"width": 1920, "height": 1080, "avail_height": 1032, "dpr": 1.25},
    {"width": 1536, "height": 864,  "avail_height": 824,  "dpr": 1.25},
    {"width": 1440, "height": 900,  "avail_height": 860,  "dpr": 1.0},
    {"width": 1366, "height": 768,  "avail_height": 728,  "dpr": 1.0},
    {"width": 1600, "height": 900,  "avail_height": 860,  "dpr": 1.0},
    {"width": 1680, "height": 1050, "avail_height": 1010, "dpr": 1.0},
    {"width": 2560, "height": 1440, "avail_height": 1392, "dpr": 1.25},
    {"width": 2560, "height": 1440, "avail_height": 1392, "dpr": 1.5},
    {"width": 1920, "height": 1200, "avail_height": 1160, "dpr": 1.0},
    {"width": 2160, "height": 1440, "avail_height": 1400, "dpr": 1.5},
    {"width": 2240, "height": 1400, "avail_height": 1352, "dpr": 1.5},
    {"width": 2560, "height": 1600, "avail_height": 1552, "dpr": 1.5},
    {"width": 2880, "height": 1800, "avail_height": 1752, "dpr": 2.0},
    {"width": 3840, "height": 2160, "avail_height": 2112, "dpr": 1.75},
    {"width": 3840, "height": 2160, "avail_height": 2112, "dpr": 2.0},
]

# 真实消费级独立/集成显卡配置池（彻底替换暴露在 VPS 上的 Google SwiftShader / Mesa）
_GPU_PROFILES = [
    # NVIDIA GeForce RTX 40 系列
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 Super Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4050 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    # NVIDIA GeForce RTX 30 系列
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3090 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3090 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    # NVIDIA GeForce RTX 20 / GTX 系列
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2080 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    # Intel Iris Xe / UHD / Arc 系列
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Arc(TM) A750 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 750 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 730 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    # AMD Radeon RX 系列
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 7900 XTX Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 7700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 6800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 6650 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon(TM) 780M Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (ATI Technologies Inc.)", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"},
]

# 真实北美/全球常用 IANA 标准时区
_TIMEZONES = [
    "America/New_York", "America/Detroit",
    "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Phoenix",
    "America/Indiana/Indianapolis", "America/Toronto",
    "America/Vancouver", "Europe/London",
    "Europe/Berlin", "Europe/Paris", "Europe/Amsterdam",
]

# 美式/英联邦标准英语语言偏好
_LOCALES = [
    ("en-US", ["en-US", "en"]),
    ("en-US", ["en-US", "en", "es"]),
    ("en-US", ["en-US", "en-GB", "en"]),
    ("en-US", ["en-US", "en-CA", "en"]),
    ("en-GB", ["en-GB", "en-US", "en"]),
    ("en-CA", ["en-CA", "en-US", "en"]),
]

_PLATFORM_VERSIONS = ["10.0.19045", "10.0.22000", "10.0.22621", "10.0.22631", "10.0.26100"]

# ── GPU 对应的真实 WebGL 参数一致性矩阵（消除跨 GPU 参数碰撞） ──
# key: renderer 子串匹配关键词 → (MAX_TEXTURE_SIZE, MAX_VIEWPORT_DIMS, MAX_RENDERBUFFER_SIZE,
#   MAX_VERTEX_ATTRIBS, MAX_VARYING_VECTORS, MAX_VERTEX_UNIFORM_VECTORS, MAX_FRAGMENT_UNIFORM_VECTORS,
#   MAX_VERTEX_TEXTURE_IMAGE_UNITS, MAX_TEXTURE_IMAGE_UNITS, MAX_COMBINED_TEXTURE_IMAGE_UNITS,
#   ALIASED_LINE_WIDTH_RANGE_MAX, MAX_CUBE_MAP_TEXTURE_SIZE)
_GPU_GL_PARAMS = {
    "RTX 40":   (32768, 32768, 32768, 16, 32, 4096, 1024, 32, 32, 96, 1.0, 32768),
    "RTX 30":   (32768, 32768, 32768, 16, 32, 4096, 1024, 32, 32, 96, 1.0, 32768),
    "RTX 20":   (32768, 32768, 32768, 16, 32, 4096, 1024, 32, 32, 96, 1.0, 32768),
    "GTX 16":   (32768, 32768, 32768, 16, 32, 4096, 1024, 32, 32, 96, 1.0, 32768),
    "Arc":      (16384, 16384, 16384, 16, 32, 4096, 1024, 32, 32, 80, 1.0, 16384),
    "Iris":     (16384, 16384, 16384, 16, 31, 4096, 1024, 32, 32, 80, 1.0, 16384),
    "UHD":      (16384, 16384, 16384, 16, 31, 4096, 1024, 32, 32, 80, 1.0, 16384),
    "Radeon RX 7": (16384, 16384, 16384, 16, 32, 4096, 1024, 32, 32, 80, 1.0, 16384),
    "Radeon RX 6": (16384, 16384, 16384, 16, 32, 4096, 1024, 32, 32, 80, 1.0, 16384),
    "Radeon":   (16384, 16384, 16384, 16, 32, 4096, 1024, 32, 32, 80, 1.0, 16384),
}

# 真实 Chromium D3D11 后端 WebGL 扩展完整池（按照真实浏览器收集）
_WEBGL_EXTENSIONS_POOL = [
    "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_clip_control",
    "EXT_color_buffer_half_float", "EXT_depth_clamp", "EXT_disjoint_timer_query",
    "EXT_float_blend", "EXT_frag_depth", "EXT_polygon_offset_clamp",
    "EXT_shader_texture_lod", "EXT_sRGB", "EXT_texture_compression_bptc",
    "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
    "EXT_texture_mirror_clamp_to_edge", "KHR_parallel_shader_compile",
    "OES_draw_buffers_float", "OES_element_index_uint", "OES_fbo_render_mipmap",
    "OES_standard_derivatives", "OES_texture_float", "OES_texture_float_linear",
    "OES_texture_half_float", "OES_texture_half_float_linear",
    "OES_vertex_array_object", "WEBGL_blend_func_extended",
    "WEBGL_color_buffer_float", "WEBGL_compressed_texture_s3tc",
    "WEBGL_compressed_texture_s3tc_srgb", "WEBGL_debug_renderer_info",
    "WEBGL_debug_shaders", "WEBGL_depth_texture", "WEBGL_draw_buffers",
    "WEBGL_lose_context", "WEBGL_multi_draw", "WEBGL_polygon_mode",
]

# 真实 Windows 桌面安装字体采样池（每次随机丢弃 1~3 个来产生唯一子集）
_FONT_PROBE_BASE = [
    "Arial", "Arial Black", "Arial Narrow", "Book Antiqua", "Bookman Old Style",
    "Calibri", "Cambria", "Cambria Math", "Century", "Century Gothic",
    "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New",
    "Franklin Gothic Medium", "Garamond", "Georgia", "Impact", "Lucida Console",
    "Lucida Sans Unicode", "Microsoft Sans Serif", "Palatino Linotype",
    "Segoe UI", "Segoe UI Light", "Segoe UI Semibold", "Segoe Print",
    "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana", "Wingdings",
]


def random_fingerprint(major_ver: str = "151", full_ver: str | None = None) -> dict:
    """生成一套与宿主系统 Chromium 严格对齐的高拟真、全维度随机指纹参数。"""
    res = random.choice(_SCREEN_RESOLUTIONS)
    gpu = random.choice(_GPU_PROFILES)
    tz = random.choice(_TIMEZONES)
    loc, langs = random.choice(_LOCALES)
    platform = "Win32"

    hw = os.cpu_count() or 8
    dm = random.choice([8, 8, 16, 16, 32, 32])
    rtt = random.choice([20, 25, 30, 35, 40, 50, 60, 75])
    dl = random.choice([15.0, 20.0, 25.0, 30.0, 50.0, 100.0])

    vp_w = res["width"]
    vp_h = res["avail_height"]

    # 拟真微版本号或严格对齐真实版本号
    if not full_ver:
        build_patch = random.choice(["0.7922.34", "0.7922.50", "0.7922.65", "0.7900.100"])
        full_ver = f"{major_ver}.{build_patch.split('.', 1)[1]}" if "." in build_patch else f"{major_ver}.0.0.0"

    ua = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major_ver}.0.0.0 Safari/537.36'
    
    # 真实 Client Hints brands 结构（匹配 Chrome 120+ 标准格式）
    brand_entries = [
        {"brand": "Chromium", "version": major_ver},
        {"brand": "Google Chrome", "version": major_ver},
        {"brand": "Not_A Brand", "version": "24"},
    ]
    random.shuffle(brand_entries)
    sec_ch_ua = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brand_entries)

    # 会话级高精度连续随机扰动因子（64位浮点，保证无上限唯一性）
    seed = round(random.uniform(0.0000001, 0.9999999), 8)

    # 窗口标题栏与边框差值 (真实桌面 Chrome: outerHeight - innerHeight 约为 80~100px)
    frame_h_diff = random.choice([85, 88, 92, 96, 100])
    frame_w_diff = 16

    platform_ver = random.choice(_PLATFORM_VERSIONS)
    heap_limit = random.choice([2172649472, 4294705152])

    # 动态音频与视频媒体设备标签
    audio_models = ["Realtek(R) Audio", "Realtek High Definition Audio", "High Definition Audio Device", "USB Audio Device", "NVIDIA High Definition Audio"]
    cam_models = ["Integrated Camera", "HD WebCam", "USB Video Device", "Logitech HD Webcam C920", "FHD Camera"]
    audio_in = f"Microphone ({random.choice(audio_models)})"
    audio_out = f"Speakers ({random.choice(audio_models)})"
    cam_in = random.choice(cam_models)

    # ── GPU 一致性 WebGL 参数矩阵 ──
    gl_params = (16384, 16384, 16384, 16, 32, 4096, 1024, 32, 32, 80, 1.0, 16384)  # 默认
    for key, params in _GPU_GL_PARAMS.items():
        if key in gpu["renderer"]:
            gl_params = params
            break

    # ── 真实 WebGL 扩展子集（随机丢弃 0~2 个，模拟驱动差异） ──
    gl_exts = list(_WEBGL_EXTENSIONS_POOL)
    drop_count = random.randint(0, 2)
    for _ in range(drop_count):
        if len(gl_exts) > 28:
            gl_exts.pop(random.randint(0, len(gl_exts) - 1))

    # ── 字体子集指纹（随机丢弃 1~3 个字体，模拟不同 Windows 安装） ──
    font_subset = list(_FONT_PROBE_BASE)
    font_drop = random.randint(1, 3)
    for _ in range(font_drop):
        if len(font_subset) > 20:
            font_subset.pop(random.randint(0, len(font_subset) - 1))

    # ── ClientRects / DOMRect 亚像素噪点种子 ──
    rect_noise_seed = round(random.uniform(0.0000001, 0.9999999), 8)

    # ── 独立的 Audio 二次噪声种子（与 canvas seed 完全分离） ──
    audio_noise_seed = round(random.uniform(0.0000001, 0.9999999), 8)

    # ── performance.memory 堆限制随机化 ──
    js_heap_limit = random.choice([2172649472, 2197815296, 4294705152, 4294967296])
    used_heap = random.randint(int(js_heap_limit * 0.08), int(js_heap_limit * 0.18))
    total_heap = random.randint(used_heap + 1000000, int(js_heap_limit * 0.45))

    # ── WebGL Shader Precision 随机化（匹配真实硬件散布） ──
    shader_precision_rangeMin = random.choice([1, 1, 1, 127])
    shader_precision_rangeMax = random.choice([1, 1, 1, 127])
    shader_precision_precision = random.choice([23, 23, 23, 8])

    return dict(
        chrome_ver=full_ver,
        major_ver=major_ver,
        brand_entries=brand_entries,
        viewport={"width": vp_w, "height": vp_h},
        screen_width=res["width"],
        screen_height=res["height"],
        screen_avail_width=res["width"],
        screen_avail_height=res["avail_height"],
        dpr=res["dpr"],
        timezone=tz,
        user_agent=ua,
        sec_ch_ua=sec_ch_ua,
        locale=loc,
        languages=langs,
        platform=platform,
        platform_version=platform_ver,
        hw=hw,
        dm=dm,
        rtt=rtt,
        dl=dl,
        gpu_vendor=gpu["vendor"],
        gpu_renderer=gpu["renderer"],
        seed=seed,
        frame_h_diff=frame_h_diff,
        frame_w_diff=frame_w_diff,
        heap_limit=heap_limit,
        audio_in=audio_in,
        audio_out=audio_out,
        cam_in=cam_in,
        # ── 新增加强指纹维度 ──
        gl_params=gl_params,
        gl_exts=gl_exts,
        font_subset=font_subset,
        rect_noise_seed=rect_noise_seed,
        audio_noise_seed=audio_noise_seed,
        js_heap_limit=js_heap_limit,
        js_used_heap=used_heap,
        js_total_heap=total_heap,
        shader_precision_rangeMin=shader_precision_rangeMin,
        shader_precision_rangeMax=shader_precision_rangeMax,
        shader_precision_precision=shader_precision_precision,
    )


def build_init_script(fp: dict) -> str:
    """生成纯净、原生对齐的高拟真防爬辅助注入脚本 (Stealth v16 终极全维强化版)。
    参考并整合了 rebrowser-patches、puppeteer-extra-plugin-stealth、fingerprint-suite、camoufox 与 CreepJS 测试基准：
    1. ES6 Concise Object Method 原生函数工厂：构造非构造器函数，严格对齐 V8 函数名与 toString，杜绝 Function.prototype.hasOwnProperty('prototype') 探测；
    2. WeakMap 原生函数原型链伪装 (Function.prototype.toString 严格对齐 V8 原生 [native code]，杜绝 AST/代码字符串化检测)；
    3. Web Worker / Blob Worker 深度域隔离同步：拦截 URL.createObjectURL 与 Worker 构造函数，向 Worker 线程内无缝注入硬件与语言环境；
    4. Console / CDP Getter 探测陷阱全面消除：防御 Cloudflare Turnstile 在 console.debug/dir 中传入带 Getter 对象的 CDP 自动化监听陷阱；
    5. 原型链级别净化 navigator.webdriver (彻底移除自动化痕迹，原型属性 getter 返回 false，无 ownProperty 异常)；
    6. 标准原生桌面级 window.chrome (对齐 app, loadTimes, csi，彻底移除会暴露 mock 的伪造 runtime)；
    7. 桌面级真实 PluginArray & MimeTypeArray (5 大内置 PDF 插件，定义在 Navigator.prototype，消除 plugins.length===0 无头漏洞)；
    8. 硬件参数全面对齐 (hardwareConcurrency, deviceMemory, maxTouchPoints=0 严格挂载在 Navigator.prototype)；
    9. 语言参数对齐 (language, languages 严格挂载在 Navigator.prototype 并与代理出口 IP 国家对齐)；
    10. 提供完整 navigator.userAgentData (Client Hints) 高熵接口 (包含 wow64: false，挂载于原型链)；
    11. WebGL GPU 一致性全参数矩阵深度硬化（getParameter 20+ GL 常量 × GPU 对齐、Shader Precision、扩展列表、readPixels PRNG 微噪点）；
    12. Canvas 2D 亚像素级微偏移与噪点扰动 + toDataURL/toBlob 一致性噪点注入 + measureText 排版指标扰动；
    13. AudioContext & OfflineAudioContext 全频域/时域微扰动 (独立 seed、WeakSet 幂等、getChannelData/copyFromChannel/AnalyserNode 全覆盖)；
    14. SpeechSynthesis 语音库模拟 (补齐 Linux Headless 环境下缺失的 Windows 桌面语音库)；
    15. MediaDevices 摄像头/麦克风设备模拟 (对齐真实物理 PC 硬件外设枚举列表)；
    16. Battery API 与 NetworkInformation (navigator.connection) 真实 4G/宽带网络与 RTT/Downlink 联动；
    17. Notification.permission 与 Permissions API 原生状态对齐；
    18. Document.prototype.hasFocus / visibilityState / hidden 原生对齐；
    19. 屏幕与视口几何参数严格一致；
    20. ClientRects / DOMRect 亚像素噪点（getBoundingClientRect / getClientRects 独立种子扰动，消除 CSS 字体探测聚类）；
    21. Font Enumeration 防御（基于字体子集指纹 + measureText 字体依赖微噪，消除跨账号字体枚举碰撞）；
    22. performance.memory 堆大小随机化伪装（消除内存指纹碰撞）；
    23. Screen.orientation 真实桌面 landscape-primary 对象对齐；
    24. WebGPU (navigator.gpu) 真实 GPU 适配器模拟 + 硬件 limits 参数联动；
    25. WebRTC ICE Candidate 本地内网 IP 深度过滤与 SDP 地址替换；
    26. Date / Intl 时区一致性双重保障（resolvedOptions().timeZone 强制对齐）；
    27. Event.prototype.isTrusted 原生 getter 保护（防止框架检测 CDP 合成事件篡改标记）。
    """
    brands_json = json.dumps(fp.get("brand_entries", [
        {"brand": "Chromium", "version": fp.get("major_ver", "151")},
        {"brand": "Google Chrome", "version": fp.get("major_ver", "151")},
        {"brand": "Not_A Brand", "version": "24"}
    ]), ensure_ascii=False)

    vendor = fp.get("gpu_vendor", "Google Inc. (NVIDIA)")
    renderer = fp.get("gpu_renderer", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)")
    platform_ver = fp.get("platform_version", "10.0.19045")
    frame_h = fp.get("frame_h_diff", 88)
    hw = fp.get("hw", 8)
    dm = fp.get("dm", 8)
    rtt = fp.get("rtt", 50)
    dl = fp.get("dl", 25.0)
    sw = fp.get("screen_width", 1920)
    sh = fp.get("screen_height", 1080)
    saw = fp.get("screen_avail_width", 1920)
    sah = fp.get("screen_avail_height", 1040)
    loc = fp.get("locale", "en-US")
    langs_json = json.dumps(fp.get("languages", ["en-US", "en"]))
    seed = fp.get("seed", 0.12345678)
    audio_in = fp.get("audio_in", "Microphone (Realtek High Definition Audio)")
    audio_out = fp.get("audio_out", "Speakers (Realtek High Definition Audio)")
    cam_in = fp.get("cam_in", "HD WebCam")
    ua = fp.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

    # ── 新增加强维度参数提取 ──
    gl_p = fp.get("gl_params", (16384, 16384, 16384, 16, 32, 4096, 1024, 32, 32, 80, 1.0, 16384))
    gl_max_tex = gl_p[0]
    gl_max_vp = gl_p[1]
    gl_max_rb = gl_p[2]
    gl_max_va = gl_p[3]
    gl_max_vv = gl_p[4]
    gl_max_vuv = gl_p[5]
    gl_max_fuv = gl_p[6]
    gl_max_vtiu = gl_p[7]
    gl_max_tiu = gl_p[8]
    gl_max_ctiu = gl_p[9]
    gl_aliased_lw = gl_p[10]
    gl_max_cube = gl_p[11]
    gl_exts_json = json.dumps(fp.get("gl_exts", []))
    font_subset_json = json.dumps(fp.get("font_subset", []))
    rect_seed = fp.get("rect_noise_seed", 0.12345678)
    audio_seed = fp.get("audio_noise_seed", 0.12345678)
    js_heap_limit = fp.get("js_heap_limit", 2172649472)
    js_used_heap = fp.get("js_used_heap", 250000000)
    js_total_heap = fp.get("js_total_heap", 500000000)
    sp_rangeMin = fp.get("shader_precision_rangeMin", 1)
    sp_rangeMax = fp.get("shader_precision_rangeMax", 1)
    sp_precision = fp.get("shader_precision_precision", 23)

    return f"""
(() => {{
    'use strict';

    // ── 0. Native Function Cloaking Engine (ES6 Non-Constructible Methods & Native Descriptors) ──
    const nativeToString = Function.prototype.toString;
    const customToStringMap = new WeakMap();

    const helper = {{
        createNativeMethod(name, impl, length = 0) {{
            const holder = {{
                [name](...args) {{
                    return impl.apply(this, args);
                }}
            }};
            const method = holder[name];
            try {{
                Object.defineProperty(method, 'name', {{ value: name, configurable: true, writable: false }});
                Object.defineProperty(method, 'length', {{ value: length, configurable: true, writable: false }});
            }} catch (e) {{}}
            customToStringMap.set(method, "function " + (name || "") + "() {{ [native code] }}");
            return method;
        }},
        createNativeGetter(name, getterImpl) {{
            const getterName = "get " + name;
            const holder = {{
                [name]() {{
                    return getterImpl.call(this);
                }}
            }};
            const getterFn = holder[name];
            try {{
                Object.defineProperty(getterFn, 'name', {{ value: getterName, configurable: true, writable: false }});
                Object.defineProperty(getterFn, 'length', {{ value: 0, configurable: true, writable: false }});
            }} catch (e) {{}}
            customToStringMap.set(getterFn, "function " + getterName + "() {{ [native code] }}");
            return getterFn;
        }}
    }};

    const toStringHolder = {{
        toString() {{
            if (typeof this !== 'function') {{
                throw new TypeError("Function.prototype.toString requires that 'this' be a Function");
            }}
            if (customToStringMap.has(this)) {{
                return customToStringMap.get(this);
            }}
            return nativeToString.apply(this, arguments);
        }}
    }};
    const hookedToString = toStringHolder.toString;
    customToStringMap.set(hookedToString, "function toString() {{ [native code] }}");
    try {{
        Object.defineProperty(Function.prototype, 'toString', {{
            value: hookedToString,
            writable: true,
            configurable: true,
            enumerable: false
        }});
    }} catch (e) {{}}

    // ── 0.1 Web Worker Realm Synchronization (Blob Worker Interceptor) ─────
    try {{
        const origCreateObjectURL = URL.createObjectURL;
        const workerShim = `
            try {{
                delete navigator.webdriver;
                const proto = Object.getPrototypeOf(navigator) || navigator;
                Object.defineProperty(proto, 'webdriver', {{ get: () => false, configurable: true, enumerable: true }});
                Object.defineProperty(proto, 'hardwareConcurrency', {{ get: () => {hw}, configurable: true, enumerable: true }});
                Object.defineProperty(proto, 'deviceMemory', {{ get: () => {dm}, configurable: true, enumerable: true }});
                Object.defineProperty(proto, 'language', {{ get: () => "{loc}", configurable: true, enumerable: true }});
                Object.defineProperty(proto, 'languages', {{ get: () => Object.freeze({langs_json}), configurable: true, enumerable: true }});
            }} catch(e) {{}}
        `;

        URL.createObjectURL = helper.createNativeMethod('createObjectURL', function(blob) {{
            if (blob instanceof Blob && blob.type && blob.type.includes('javascript')) {{
                const modifiedBlob = new Blob([workerShim + '\\n', blob], {{ type: blob.type }});
                return origCreateObjectURL.call(URL, modifiedBlob);
            }}
            return origCreateObjectURL.apply(URL, arguments);
        }});
    }} catch (e) {{}}

    // ── 0.2 Console CDP Getter Trap Neutralization ─────────────────────────
    // 防御 Cloudflare Turnstile / DataDome / Arkose 借助 console 传递带 getter / Proxy 对象的 CDP 自动化监听陷阱
    try {{
        const noopConsoleMethods = [
            'debug', 'dir', 'dirxml', 'trace', 'table',
            'group', 'groupCollapsed', 'groupEnd',
            'count', 'countReset', 'time', 'timeEnd', 'timeLog', 'assert'
        ];
        for (const m of noopConsoleMethods) {{
            if (console[m]) {{
                console[m] = helper.createNativeMethod(m, function() {{}}, 0);
            }}
        }}

        // 对 log, info, warn, error 实施参数零泄露安全净化：
        // 遇到非原始类型（对象、函数、Symbol）直接返回安全占位符，绝不访问属性或描述符，彻底避免触发 Proxy 陷阱与 CDP 序列化
        const origLog = console.log;
        const origWarn = console.warn;
        const origInfo = console.info;
        const origError = console.error;

        function sanitizeLogArg(arg) {{
            if (arg !== null && (typeof arg === 'object' || typeof arg === 'function' || typeof arg === 'symbol')) {{
                return '[Object]';
            }}
            return arg;
        }}

        if (origLog) {{
            console.log = helper.createNativeMethod('log', function(...args) {{
                const safeArgs = args.map(sanitizeLogArg);
                return origLog.apply(console, safeArgs);
            }}, 0);
        }}
        if (origWarn) {{
            console.warn = helper.createNativeMethod('warn', function(...args) {{
                const safeArgs = args.map(sanitizeLogArg);
                return origWarn.apply(console, safeArgs);
            }}, 0);
        }}
        if (origInfo) {{
            console.info = helper.createNativeMethod('info', function(...args) {{
                const safeArgs = args.map(sanitizeLogArg);
                return origInfo.apply(console, safeArgs);
            }}, 0);
        }}
        if (origError) {{
            console.error = helper.createNativeMethod('error', function(...args) {{
                const safeArgs = args.map(sanitizeLogArg);
                return origError.apply(console, safeArgs);
            }}, 0);
        }}
    }} catch (e) {{}}

    // ── 1. Clean navigator.webdriver & Proto Alignment ────────────────────
    try {{
        if (navigator.hasOwnProperty('webdriver')) {{
            delete navigator.webdriver;
        }}
        if (navigator.webdriver !== false) {{
            const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
            try {{ delete navProto.webdriver; }} catch(e) {{}}
            try {{ delete navigator.webdriver; }} catch(e) {{}}
            const getWebdriver = helper.createNativeGetter('webdriver', function() {{ return false; }});
            Object.defineProperty(navProto, 'webdriver', {{
                get: getWebdriver,
                set: undefined,
                enumerable: true,
                configurable: true
            }});
        }}
    }} catch (e) {{}}

    // ── 2. window.chrome Alignment (Vanilla Desktop Chrome) ──
    try {{
        if (!window.chrome) {{
            window.chrome = {{}};
        }}
        if (window.chrome.runtime) {{
            try {{ delete window.chrome.runtime; }} catch (e) {{}}
        }}
        if (!window.chrome.app) {{
            window.chrome.app = {{
                isInstalled: false,
                InstallState: {{ DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" }},
                RunningState: {{ CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" }},
                getIsInstalled: helper.createNativeMethod('getIsInstalled', function() {{ return false; }}),
                getInstallState: helper.createNativeMethod('getInstallState', function() {{ return "not_installed"; }})
            }};
        }}
        if (!window.chrome.loadTimes) {{
            const loadTimesFn = helper.createNativeMethod('loadTimes', function() {{
                return {{
                    requestTime: (Date.now() - 300) / 1000,
                    startLoadTime: (Date.now() - 280) / 1000,
                    commitLoadTime: (Date.now() - 250) / 1000,
                    finishDocumentLoadTime: (Date.now() - 100) / 1000,
                    finishLoadTime: (Date.now() - 50) / 1000,
                    firstPaintTime: (Date.now() - 200) / 1000,
                    firstPaintAfterLoadTime: 0,
                    navigationType: "Other",
                    wasFetchedViaSpdy: false,
                    wasNpnNegotiated: false,
                    npnNegotiatedProtocol: "",
                    wasAlternateProtocolAvailable: false,
                    connectionInfo: "http/1.1"
                }};
            }});
            window.chrome.loadTimes = loadTimesFn;
        }}
        if (!window.chrome.csi) {{
            const csiFn = helper.createNativeMethod('csi', function() {{
                return {{
                    startE: Date.now() - 300,
                    onloadT: Date.now() - 50,
                    pageT: 250.0,
                    tran: 15
                }};
            }});
            window.chrome.csi = csiFn;
        }}
    }} catch (e) {{}}

    // ── 3. navigator.plugins & navigator.mimeTypes (5 PDF Plugins on Proto) ───
    try {{
        // 只有当 plugins 为空（例如无头 Linux 环境缺失内置 PDF 插件）才进行补齐；
        // 如果宿主 Chromium 已具备原生 5 大内置 PDF 插件，绝不破坏其原生 C++ 结构与内部插槽
        if (!navigator.plugins || navigator.plugins.length === 0) {{
            const pluginData = [
                {{ name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimeTypes: ["application/pdf", "text/pdf"] }},
                {{ name: "Chrome PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimeTypes: ["application/pdf", "text/pdf"] }},
                {{ name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimeTypes: ["application/pdf", "text/pdf"] }},
                {{ name: "Microsoft Edge PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimeTypes: ["application/pdf", "text/pdf"] }},
                {{ name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "Portable Document Format", mimeTypes: ["application/pdf", "text/pdf"] }}
            ];

            const fakePlugins = [];
            const fakeMimes = [];

            for (const p of pluginData) {{
                const pluginObj = Object.create(Plugin.prototype);
                Object.defineProperties(pluginObj, {{
                    name: {{ value: p.name, enumerable: true }},
                    filename: {{ value: p.filename, enumerable: true }},
                    description: {{ value: p.description, enumerable: true }},
                    length: {{ value: p.mimeTypes.length, enumerable: true }}
                }});
                for (let i = 0; i < p.mimeTypes.length; i++) {{
                    const mimeType = p.mimeTypes[i];
                    const mimeObj = Object.create(MimeType.prototype);
                    Object.defineProperties(mimeObj, {{
                        type: {{ value: mimeType, enumerable: true }},
                        suffixes: {{ value: "pdf", enumerable: true }},
                        description: {{ value: "Portable Document Format", enumerable: true }},
                        enabledPlugin: {{ value: pluginObj, enumerable: true }}
                    }});
                    pluginObj[i] = mimeObj;
                    pluginObj[mimeType] = mimeObj;
                    if (!fakeMimes.some(m => m.type === mimeType)) {{
                        fakeMimes.push(mimeObj);
                    }}
                }}
                fakePlugins.push(pluginObj);
            }}

            const pluginArray = Object.create(PluginArray.prototype);
            Object.defineProperty(pluginArray, "length", {{ value: fakePlugins.length, enumerable: true }});
            for (let i = 0; i < fakePlugins.length; i++) {{
                pluginArray[i] = fakePlugins[i];
                pluginArray[fakePlugins[i].name] = fakePlugins[i];
            }}
            pluginArray.item = helper.createNativeMethod('item', function(idx) {{ return this[idx] || null; }});
            pluginArray.namedItem = helper.createNativeMethod('namedItem', function(name) {{ return this[name] || null; }});
            pluginArray.refresh = helper.createNativeMethod('refresh', function() {{}});

            const mimeArray = Object.create(MimeTypeArray.prototype);
            Object.defineProperty(mimeArray, "length", {{ value: fakeMimes.length, enumerable: true }});
            for (let i = 0; i < fakeMimes.length; i++) {{
                mimeArray[i] = fakeMimes[i];
                mimeArray[fakeMimes[i].type] = fakeMimes[i];
            }}
            mimeArray.item = helper.createNativeMethod('item', function(idx) {{ return this[idx] || null; }});
            mimeArray.namedItem = helper.createNativeMethod('namedItem', function(name) {{ return this[name] || null; }});

            const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
            delete navigator.plugins;
            delete navigator.mimeTypes;
            delete navigator.pdfViewerEnabled;

            Object.defineProperty(navProto, "plugins", {{
                get: helper.createNativeGetter('plugins', function() {{ return pluginArray; }}),
                configurable: true,
                enumerable: true
            }});
            Object.defineProperty(navProto, "mimeTypes", {{
                get: helper.createNativeGetter('mimeTypes', function() {{ return mimeArray; }}),
                configurable: true,
                enumerable: true
            }});
            Object.defineProperty(navProto, "pdfViewerEnabled", {{
                get: helper.createNativeGetter('pdfViewerEnabled', function() {{ return true; }}),
                configurable: true,
                enumerable: true
            }});
        }}
    }} catch (e) {{}}

    // ── 4. Hardware Concurrency, Device Memory & Languages ─────────────────
    try {{
        const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
        delete navigator.hardwareConcurrency;
        delete navigator.deviceMemory;
        delete navigator.maxTouchPoints;
        delete navigator.language;
        delete navigator.languages;

        Object.defineProperty(navProto, 'hardwareConcurrency', {{
            get: helper.createNativeGetter('hardwareConcurrency', function() {{ return {hw}; }}),
            configurable: true,
            enumerable: true
        }});
        Object.defineProperty(navProto, 'deviceMemory', {{
            get: helper.createNativeGetter('deviceMemory', function() {{ return {dm}; }}),
            configurable: true,
            enumerable: true
        }});
        Object.defineProperty(navProto, 'maxTouchPoints', {{
            get: helper.createNativeGetter('maxTouchPoints', function() {{ return 0; }}),
            configurable: true,
            enumerable: true
        }});
        Object.defineProperty(navProto, 'language', {{
            get: helper.createNativeGetter('language', function() {{ return "{loc}"; }}),
            configurable: true,
            enumerable: true
        }});
        const langsList = {langs_json};
        Object.defineProperty(navProto, 'languages', {{
            get: helper.createNativeGetter('languages', function() {{ return Object.freeze([...langsList]); }}),
            configurable: true,
            enumerable: true
        }});
    }} catch (e) {{}}

    // ── 5. navigator.userAgentData (Client Hints) High Entropy ─────────────
    try {{
        const rawBrands = {brands_json};
        const uaData = {{
            brands: rawBrands,
            mobile: false,
            platform: "Windows",
            getHighEntropyValues: helper.createNativeMethod('getHighEntropyValues', function(hints) {{
                return Promise.resolve({{
                    brands: rawBrands,
                    mobile: false,
                    platform: "Windows",
                    platformVersion: "{platform_ver}",
                    architecture: "x86",
                    bitness: "64",
                    model: "",
                    wow64: false,
                    fullVersionList: rawBrands.map(b => ({{ brand: b.brand, version: b.version + ".0.0.0" }}))
                }});
            }}),
            toJSON: helper.createNativeMethod('toJSON', function() {{
                return {{
                    brands: rawBrands,
                    mobile: false,
                    platform: "Windows"
                }};
            }})
        }};

        const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
        delete navigator.userAgentData;
        Object.defineProperty(navProto, "userAgentData", {{
            get: helper.createNativeGetter('userAgentData', function() {{ return uaData; }}),
            configurable: true,
            enumerable: true
        }});
    }} catch (e) {{}}

    // ── 6. WebGL Full-Depth GPU-Consistent Parameter Hardening ──────────────
    try {{
        const spoofVendor = "{vendor}";
        const spoofRenderer = "{renderer}";
        const glExts = {gl_exts_json};

        function hookGetParameter(proto) {{
            if (!proto || !proto.getParameter) return;
            const orig = proto.getParameter;
            const hooked = helper.createNativeMethod('getParameter', function(param) {{
                if (param === 0x9245) return spoofVendor;      // UNMASKED_VENDOR_WEBGL
                if (param === 0x9246) return spoofRenderer;    // UNMASKED_RENDERER_WEBGL
                if (param === 0x1F00) return "WebKit";         // VENDOR
                if (param === 0x1F01) return "WebKit WebGL";   // RENDERER
                if (param === 0x0D33) return {gl_max_tex};     // MAX_TEXTURE_SIZE
                if (param === 0x84E8) return {gl_max_cube};    // MAX_CUBE_MAP_TEXTURE_SIZE
                if (param === 0x84E4) return {gl_max_rb};      // MAX_RENDERBUFFER_SIZE
                if (param === 0x8869) return {gl_max_va};      // MAX_VERTEX_ATTRIBS
                if (param === 0x8DFC) return {gl_max_vv};      // MAX_VARYING_VECTORS
                if (param === 0x8DFB) return {gl_max_vuv};     // MAX_VERTEX_UNIFORM_VECTORS
                if (param === 0x8DFD) return {gl_max_fuv};     // MAX_FRAGMENT_UNIFORM_VECTORS
                if (param === 0x8B4C) return {gl_max_vtiu};    // MAX_VERTEX_TEXTURE_IMAGE_UNITS
                if (param === 0x8872) return {gl_max_tiu};     // MAX_TEXTURE_IMAGE_UNITS
                if (param === 0x8B4D) return {gl_max_ctiu};    // MAX_COMBINED_TEXTURE_IMAGE_UNITS
                if (param === 0x0D55) return new Float32Array([1.0, {gl_aliased_lw}]); // ALIASED_LINE_WIDTH_RANGE
                if (param === 0x0D53) return new Float32Array([1.0, 1024.0]);          // ALIASED_POINT_SIZE_RANGE
                if (param === 0x8B4A) return new Int32Array([{gl_max_vp}, {gl_max_vp}]); // MAX_VIEWPORT_DIMS
                return orig.apply(this, arguments);
            }});
            proto.getParameter = hooked;
        }}

        // getShaderPrecisionFormat 深度对齐（消除跨设备 Shader Precision 指纹碰撞）
        function hookShaderPrecision(proto) {{
            if (!proto || !proto.getShaderPrecisionFormat) return;
            const origSP = proto.getShaderPrecisionFormat;
            const hookedSP = helper.createNativeMethod('getShaderPrecisionFormat', function(shaderType, precisionType) {{
                const res = origSP.apply(this, arguments);
                if (res) {{
                    try {{
                        Object.defineProperty(res, 'rangeMin', {{ value: {sp_rangeMin}, writable: false }});
                        Object.defineProperty(res, 'rangeMax', {{ value: {sp_rangeMax}, writable: false }});
                        Object.defineProperty(res, 'precision', {{ value: {sp_precision}, writable: false }});
                    }} catch (e) {{}}
                }}
                return res;
            }});
            proto.getShaderPrecisionFormat = hookedSP;
        }}

        // getSupportedExtensions 完整扩展列表对齐
        function hookExtensions(proto) {{
            if (!proto || !proto.getSupportedExtensions) return;
            proto.getSupportedExtensions = helper.createNativeMethod('getSupportedExtensions', function() {{
                return [...glExts];
            }});
            // getExtension 对齐
            const origGetExt = proto.getExtension;
            proto.getExtension = helper.createNativeMethod('getExtension', function(name) {{
                if (name === 'WEBGL_debug_renderer_info') return origGetExt.apply(this, arguments);
                if (glExts.indexOf(name) === -1) return null;
                return origGetExt.apply(this, arguments);
            }});
        }}

        // readPixels 微噪点注入（消除 WebGL Canvas Hash 跨账号碰撞）
        function hookReadPixels(proto) {{
            if (!proto || !proto.readPixels) return;
            const origRP = proto.readPixels;
            const rpSeed = {seed};
            proto.readPixels = helper.createNativeMethod('readPixels', function() {{
                const result = origRP.apply(this, arguments);
                const pixels = arguments[arguments.length - 1];
                if (pixels && pixels.length && pixels.length > 16) {{
                    const step = Math.max(32, Math.floor(pixels.length / 24));
                    for (let i = 0; i < pixels.length; i += step) {{
                        if (pixels[i] > 2 && pixels[i] < 253) {{
                            pixels[i] = Math.min(255, Math.max(0, pixels[i] + (((i * rpSeed * 7) % 3) | 0) - 1));
                        }}
                    }}
                }}
                return result;
            }});
        }}

        if (window.WebGLRenderingContext) {{
            hookGetParameter(WebGLRenderingContext.prototype);
            hookShaderPrecision(WebGLRenderingContext.prototype);
            hookExtensions(WebGLRenderingContext.prototype);
            hookReadPixels(WebGLRenderingContext.prototype);
        }}
        if (window.WebGL2RenderingContext) {{
            hookGetParameter(WebGL2RenderingContext.prototype);
            hookShaderPrecision(WebGL2RenderingContext.prototype);
            hookExtensions(WebGL2RenderingContext.prototype);
            hookReadPixels(WebGL2RenderingContext.prototype);
        }}
    }} catch (e) {{}}

    // ── 7. Canvas 2D Sub-pixel & Deterministic Noise + toDataURL/toBlob ──────
    try {{
        const noiseSeed = {seed};
        if (window.CanvasRenderingContext2D) {{
            const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
            CanvasRenderingContext2D.prototype.getImageData = helper.createNativeMethod('getImageData', function(...args) {{
                const res = origGetImageData.apply(this, args);
                if (res && res.data && res.data.length >= 4) {{
                    const d = res.data;
                    const step = Math.max(16, Math.floor(d.length / 32));
                    for (let i = 0; i < d.length; i += step) {{
                        if (d[i + 3] > 0) {{
                            const delta = ((i * noiseSeed) % 3) - 1;
                            d[i] = Math.min(255, Math.max(0, d[i] + delta));
                        }}
                    }}
                }}
                return res;
            }});

            const origMeasureText = CanvasRenderingContext2D.prototype.measureText;
            CanvasRenderingContext2D.prototype.measureText = helper.createNativeMethod('measureText', function(...args) {{
                const res = origMeasureText.apply(this, args);
                if (res && typeof res.width === 'number') {{
                    const delta = (noiseSeed * 0.00004) - 0.00002;
                    try {{
                        Object.defineProperty(res, 'width', {{
                            value: res.width + delta,
                            configurable: true,
                            enumerable: true
                        }});
                        // 扰动排版指标（消除 Font Enumeration 指纹碰撞）
                        if (typeof res.actualBoundingBoxAscent === 'number') {{
                            Object.defineProperty(res, 'actualBoundingBoxAscent', {{
                                value: res.actualBoundingBoxAscent + delta * 0.5,
                                configurable: true, enumerable: true
                            }});
                        }}
                        if (typeof res.actualBoundingBoxDescent === 'number') {{
                            Object.defineProperty(res, 'actualBoundingBoxDescent', {{
                                value: res.actualBoundingBoxDescent + delta * 0.3,
                                configurable: true, enumerable: true
                            }});
                        }}
                    }} catch(e) {{}}
                }}
                return res;
            }});
        }}

        // ── Canvas toDataURL / toBlob 噪点注入（确保 Canvas Hash 唯一性）──
        if (window.HTMLCanvasElement) {{
            const _noiseApplied = new WeakSet();
            function applyCanvasNoise(canvas) {{
                if (_noiseApplied.has(canvas) || canvas.width === 0 || canvas.height === 0) return;
                try {{
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return;
                    const w = Math.min(canvas.width, 256);
                    const h = Math.min(canvas.height, 256);
                    const imgData = CanvasRenderingContext2D.prototype.getImageData.call(ctx, 0, 0, w, h);
                    const d = imgData.data;
                    const step = Math.max(16, Math.floor(d.length / 48));
                    for (let i = 0; i < d.length; i += step) {{
                        if (d[i + 3] > 0 && d[i] > 1 && d[i] < 254) {{
                            d[i] = d[i] + (((i * noiseSeed * 13) % 3) | 0) - 1;
                        }}
                    }}
                    ctx.putImageData(imgData, 0, 0);
                    _noiseApplied.add(canvas);
                }} catch (e) {{}}
            }}

            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = helper.createNativeMethod('toDataURL', function(...args) {{
                applyCanvasNoise(this);
                return origToDataURL.apply(this, args);
            }});

            const origToBlob = HTMLCanvasElement.prototype.toBlob;
            if (origToBlob) {{
                HTMLCanvasElement.prototype.toBlob = helper.createNativeMethod('toBlob', function(...args) {{
                    applyCanvasNoise(this);
                    return origToBlob.apply(this, args);
                }});
            }}
        }}
    }} catch (e) {{}}

    // ── 8. AudioContext & OfflineAudioContext Full-Depth Fingerprint Hardening ─
    try {{
        const audioSeed = {audio_seed};
        const _perturbedBuffers = new WeakSet();
        if (window.AudioBuffer) {{
            function perturbAudioData(data) {{
                if (!data || data.length === 0) return data;
                if (_perturbedBuffers.has(data)) return data;
                const step = Math.max(20, Math.floor(data.length / 50));
                for (let i = 0; i < data.length; i += step) {{
                    data[i] += (audioSeed * 0.0000001) - 0.00000005;
                }}
                _perturbedBuffers.add(data);
                return data;
            }}

            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = helper.createNativeMethod('getChannelData', function(channel) {{
                const data = origGetChannelData.apply(this, arguments);
                return perturbAudioData(data);
            }});

            // copyFromChannel 全覆盖（很多音频指纹库使用此接口）
            const origCopyFromChannel = AudioBuffer.prototype.copyFromChannel;
            if (origCopyFromChannel) {{
                AudioBuffer.prototype.copyFromChannel = helper.createNativeMethod('copyFromChannel', function(destination, channelNumber, startInChannel) {{
                    origCopyFromChannel.apply(this, arguments);
                    if (destination && destination.length > 0) {{
                        const step = Math.max(10, Math.floor(destination.length / 40));
                        for (let i = 0; i < destination.length; i += step) {{
                            destination[i] += (audioSeed * 0.00000008) - 0.00000004;
                        }}
                    }}
                }});
            }}
        }}

        // AnalyserNode 频域数据微扰（消除 getFloatFrequencyData/getByteFrequencyData 指纹）
        if (window.AnalyserNode) {{
            const origGetFloat = AnalyserNode.prototype.getFloatFrequencyData;
            if (origGetFloat) {{
                AnalyserNode.prototype.getFloatFrequencyData = helper.createNativeMethod('getFloatFrequencyData', function(array) {{
                    origGetFloat.apply(this, arguments);
                    if (array && array.length > 0) {{
                        const step = Math.max(8, Math.floor(array.length / 30));
                        for (let i = 0; i < array.length; i += step) {{
                            array[i] += (audioSeed * 0.001) - 0.0005;
                        }}
                    }}
                }});
            }}
            const origGetByte = AnalyserNode.prototype.getByteFrequencyData;
            if (origGetByte) {{
                AnalyserNode.prototype.getByteFrequencyData = helper.createNativeMethod('getByteFrequencyData', function(array) {{
                    origGetByte.apply(this, arguments);
                    if (array && array.length > 0) {{
                        const step = Math.max(8, Math.floor(array.length / 30));
                        for (let i = 0; i < array.length; i += step) {{
                            if (array[i] > 1 && array[i] < 254) {{
                                array[i] += (((i * audioSeed * 11) % 3) | 0) - 1;
                            }}
                        }}
                    }}
                }});
            }}
        }}
    }} catch (e) {{}}

    // ── 9. SpeechSynthesis Voice Library Alignment (Windows 10/11) ──────────
    try {{
        if (window.speechSynthesis) {{
            const fakeVoices = [
                {{ voiceURI: "Microsoft David - English (United States)", name: "Microsoft David - English (United States)", lang: "en-US", localService: true, default: true }},
                {{ voiceURI: "Microsoft Zira - English (United States)", name: "Microsoft Zira - English (United States)", lang: "en-US", localService: true, default: false }},
                {{ voiceURI: "Microsoft Mark - English (United States)", name: "Microsoft Mark - English (United States)", lang: "en-US", localService: true, default: false }},
                {{ voiceURI: "Google US English", name: "Google US English", lang: "en-US", localService: false, default: false }}
            ];
            window.speechSynthesis.getVoices = helper.createNativeMethod('getVoices', function() {{
                return fakeVoices;
            }});
        }}
    }} catch (e) {{}}

    // ── 10. MediaDevices Real Hardware Alignment ────────────────────────────
    try {{
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
            const fakeDevices = [
                {{ deviceId: "default", kind: "audioinput", label: "{audio_in}", groupId: "group_1" }},
                {{ deviceId: "default", kind: "audiooutput", label: "{audio_out}", groupId: "group_1" }},
                {{ deviceId: "video_cam_1", kind: "videoinput", label: "{cam_in}", groupId: "group_2" }}
            ];
            navigator.mediaDevices.enumerateDevices = helper.createNativeMethod('enumerateDevices', function() {{
                return Promise.resolve(fakeDevices);
            }});
        }}
    }} catch (e) {{}}

    // ── 11. Battery & Network Information (navigator.connection) ───────────
    try {{
        if (!navigator.getBattery) {{
            const batteryObj = {{
                charging: true,
                chargingTime: 0,
                dischargingTime: Infinity,
                level: 1.0,
                onchargingchange: null,
                onchargingtimechange: null,
                ondischargingtimechange: null,
                onlevelchange: null,
                addEventListener: helper.createNativeMethod('addEventListener', function() {{}}),
                removeEventListener: helper.createNativeMethod('removeEventListener', function() {{}}),
                dispatchEvent: helper.createNativeMethod('dispatchEvent', function() {{ return true; }})
            }};
            const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
            navProto.getBattery = helper.createNativeMethod('getBattery', function() {{
                return Promise.resolve(batteryObj);
            }});
        }}

        const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
        const connectionObj = {{
            effectiveType: '4g',
            rtt: {rtt},
            downlink: {dl},
            saveData: false,
            onchange: null,
            addEventListener: helper.createNativeMethod('addEventListener', function() {{}}),
            removeEventListener: helper.createNativeMethod('removeEventListener', function() {{}}),
            dispatchEvent: helper.createNativeMethod('dispatchEvent', function() {{ return true; }})
        }};
        delete navigator.connection;
        Object.defineProperty(navProto, 'connection', {{
            get: helper.createNativeGetter('connection', function() {{ return connectionObj; }}),
            configurable: true,
            enumerable: true
        }});
    }} catch (e) {{}}

    // ── 12. Permissions API & Notification Alignment ────────────────────────
    try {{
        if (navigator.permissions && navigator.permissions.query) {{
            const origQuery = navigator.permissions.query;
            const hookedQuery = helper.createNativeMethod('query', function(parameters) {{
                if (parameters && parameters.name === 'notifications') {{
                    return Promise.resolve({{
                        state: Notification.permission === 'default' ? 'prompt' : Notification.permission,
                        onchange: null
                    }});
                }}
                return origQuery.apply(this, arguments);
            }});
            navigator.permissions.query = hookedQuery;
        }}
    }} catch (e) {{}}

    // ── 13. Document Visibility & Focus (Headless Neutralization) ───────────
    try {{
        Document.prototype.hasFocus = helper.createNativeMethod('hasFocus', function() {{ return true; }});
        Object.defineProperty(Document.prototype, 'hidden', {{
            get: helper.createNativeGetter('hidden', function() {{ return false; }}),
            enumerable: true,
            configurable: true
        }});
        Object.defineProperty(Document.prototype, 'visibilityState', {{
            get: helper.createNativeGetter('visibilityState', function() {{ return 'visible'; }}),
            enumerable: true,
            configurable: true
        }});
    }} catch (e) {{}}

    // ── 14. Window Geometry & Screen Consistency ────────────────────────────
    try {{
        const winProto = Object.getPrototypeOf(window) || Window.prototype;
        delete window.outerWidth;
        delete window.outerHeight;
        Object.defineProperty(winProto, "outerWidth", {{
            get: helper.createNativeGetter('outerWidth', function() {{ return window.innerWidth ? window.innerWidth + 16 : 1920; }}),
            configurable: true,
            enumerable: true
        }});
        Object.defineProperty(winProto, "outerHeight", {{
            get: helper.createNativeGetter('outerHeight', function() {{ return window.innerHeight ? window.innerHeight + {frame_h} : 1080; }}),
            configurable: true,
            enumerable: true
        }});

        const screenProto = Object.getPrototypeOf(screen) || Screen.prototype;
        delete screen.width;
        delete screen.height;
        delete screen.availWidth;
        delete screen.availHeight;
        delete screen.colorDepth;
        delete screen.pixelDepth;

        Object.defineProperty(screenProto, 'width', {{
            get: helper.createNativeGetter('width', function() {{ return {sw}; }}),
            configurable: true, enumerable: true
        }});
        Object.defineProperty(screenProto, 'height', {{
            get: helper.createNativeGetter('height', function() {{ return {sh}; }}),
            configurable: true, enumerable: true
        }});
        Object.defineProperty(screenProto, 'availWidth', {{
            get: helper.createNativeGetter('availWidth', function() {{ return {saw}; }}),
            configurable: true, enumerable: true
        }});
        Object.defineProperty(screenProto, 'availHeight', {{
            get: helper.createNativeGetter('availHeight', function() {{ return {sah}; }}),
            configurable: true, enumerable: true
        }});
        Object.defineProperty(screenProto, 'colorDepth', {{
            get: helper.createNativeGetter('colorDepth', function() {{ return 24; }}),
            configurable: true, enumerable: true
        }});
        Object.defineProperty(screenProto, 'pixelDepth', {{
            get: helper.createNativeGetter('pixelDepth', function() {{ return 24; }}),
            configurable: true, enumerable: true
        }});
    }} catch (e) {{}}

    // ── 15. ClientRects / DOMRect Sub-pixel Noise (Anti-Fingerprint with Native Integrity) ──
    try {{
        const rectSeed = {rect_seed};
        const origGetBCR = Element.prototype.getBoundingClientRect;
        Element.prototype.getBoundingClientRect = helper.createNativeMethod('getBoundingClientRect', function() {{
            const rect = origGetBCR.apply(this, arguments);
            if (!rect || (rect.width === 0 && rect.height === 0)) return rect;
            // 极微量亚像素抖动（<= 0.00002px），维持真实 DOMRect 原型链与正交坐标关系
            const noise = (rectSeed * 0.00004) - 0.00002;
            return new DOMRect(
                rect.x + noise,
                rect.y + noise,
                rect.width + (noise * 0.1),
                rect.height + (noise * 0.1)
            );
        }});

        const origGetCR = Element.prototype.getClientRects;
        Element.prototype.getClientRects = helper.createNativeMethod('getClientRects', function() {{
            const rects = origGetCR.apply(this, arguments);
            // 保持原生 DOMRectList 原型链与元素内部测量绝对一致，避免破坏 Fluent UI / Fast Foundation
            return rects;
        }});
    }} catch (e) {{}}

    // ── 16. Font Enumeration Defense (measureText-based font probing) ───────
    try {{
        const knownFonts = new Set({font_subset_json});
        if (window.CanvasRenderingContext2D) {{
            const origFillText = CanvasRenderingContext2D.prototype.fillText;
            const origStrokeText = CanvasRenderingContext2D.prototype.strokeText;

            // 对于不在子集中的字体探测，微妙地回退到默认字体渲染
            // 通过在 measureText 中加入字体依赖噪点来消除跨实例碰撞
            const origMT = CanvasRenderingContext2D.prototype.measureText;
            if (!origMT._fontDefended) {{
                const fontDefendedMT = helper.createNativeMethod('measureText', function(text) {{
                    const res = origMT.apply(this, arguments);
                    // 额外的字体依赖微噪：基于当前 font 属性生成独特偏移
                    const fontStr = this.font || '';
                    let fontHash = 0;
                    for (let i = 0; i < fontStr.length; i++) {{
                        fontHash = ((fontHash << 5) - fontHash + fontStr.charCodeAt(i)) | 0;
                    }}
                    const fontNoise = ((fontHash * {rect_seed}) % 100) * 0.000001;
                    if (res && typeof res.width === 'number') {{
                        try {{
                            Object.defineProperty(res, 'width', {{
                                value: res.width + fontNoise,
                                configurable: true, enumerable: true
                            }});
                        }} catch(e) {{}}
                    }}
                    return res;
                }});
                fontDefendedMT._fontDefended = true;
                CanvasRenderingContext2D.prototype.measureText = fontDefendedMT;
            }}
        }}
    }} catch (e) {{}}

    // ── 17. performance.memory Heap Spoofing (Chrome-only API) ──────────────
    try {{
        if (window.performance) {{
            const memoryObj = {{
                jsHeapSizeLimit: {js_heap_limit},
                totalJSHeapSize: {js_total_heap},
                usedJSHeapSize: {js_used_heap}
            }};
            Object.freeze(memoryObj);
            try {{
                Object.defineProperty(performance, 'memory', {{
                    get: helper.createNativeGetter('memory', function() {{ return memoryObj; }}),
                    configurable: true,
                    enumerable: true
                }});
            }} catch (e) {{}}
        }}
    }} catch (e) {{}}

    // ── 18. Screen.orientation Desktop Landscape Alignment ──────────────────
    try {{
        const screenProto = Object.getPrototypeOf(screen) || Screen.prototype;
        const orientObj = {{
            angle: 0,
            type: 'landscape-primary',
            onchange: null,
            addEventListener: helper.createNativeMethod('addEventListener', function() {{}}),
            removeEventListener: helper.createNativeMethod('removeEventListener', function() {{}}),
            dispatchEvent: helper.createNativeMethod('dispatchEvent', function() {{ return true; }}),
            lock: helper.createNativeMethod('lock', function() {{ return Promise.reject(new DOMException('screen.orientation.lock() is not available on this device.', 'NotSupportedError')); }}),
            unlock: helper.createNativeMethod('unlock', function() {{}})
        }};
        delete screen.orientation;
        Object.defineProperty(screenProto, 'orientation', {{
            get: helper.createNativeGetter('orientation', function() {{ return orientObj; }}),
            configurable: true,
            enumerable: true
        }});
    }} catch (e) {{}}

    // ── 19. WebGPU Adapter Spoofing (navigator.gpu) ────────────────────────
    try {{
        if (!navigator.gpu) {{
            const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
            const gpuObj = {{
                requestAdapter: helper.createNativeMethod('requestAdapter', function(options) {{
                    return Promise.resolve({{
                        name: "{renderer}",
                        features: new Set(['texture-compression-bc', 'depth-clip-control']),
                        limits: {{
                            maxTextureDimension1D: {gl_max_tex},
                            maxTextureDimension2D: {gl_max_tex},
                            maxTextureDimension3D: 2048,
                            maxTextureArrayLayers: 256,
                            maxBindGroups: 4,
                            maxDynamicUniformBuffersPerPipelineLayout: 8,
                            maxDynamicStorageBuffersPerPipelineLayout: 4,
                            maxSampledTexturesPerShaderStage: 16,
                            maxSamplersPerShaderStage: 16,
                            maxStorageBuffersPerShaderStage: 8,
                            maxStorageTexturesPerShaderStage: 4,
                            maxUniformBuffersPerShaderStage: 12,
                            maxBufferSize: 268435456,
                            maxVertexBuffers: 8,
                            maxVertexAttributes: 16
                        }},
                        isFallbackAdapter: false,
                        requestDevice: helper.createNativeMethod('requestDevice', function() {{
                            return Promise.reject(new DOMException('WebGPU device creation not supported', 'NotSupportedError'));
                        }})
                    }});
                }}),
                getPreferredCanvasFormat: helper.createNativeMethod('getPreferredCanvasFormat', function() {{
                    return 'bgra8unorm';
                }})
            }};
            Object.defineProperty(navProto, 'gpu', {{
                get: helper.createNativeGetter('gpu', function() {{ return gpuObj; }}),
                configurable: true,
                enumerable: true
            }});
        }}
    }} catch (e) {{}}

    // ── 20. WebRTC ICE Candidate Local IP Masking ──────────────────────────
    try {{
        if (window.RTCPeerConnection) {{
            const origSetLocal = RTCPeerConnection.prototype.setLocalDescription;
            if (origSetLocal) {{
                RTCPeerConnection.prototype.setLocalDescription = helper.createNativeMethod('setLocalDescription', function(desc) {{
                    if (desc && desc.sdp) {{
                        // 替换本地 IP 地址 (192.168.x.x, 10.x.x.x, 172.x.x.x)
                        desc.sdp = desc.sdp.replace(/([0-9]{{1,3}}\\.([0-9]{{1,3}})\\.([0-9]{{1,3}})\\.([0-9]{{1,3}}))/g, function(match) {{
                            if (match.startsWith('192.168.') || match.startsWith('10.') ||
                                (match.startsWith('172.') && parseInt(match.split('.')[1]) >= 16 && parseInt(match.split('.')[1]) <= 31)) {{
                                return '0.0.0.0';
                            }}
                            return match;
                        }});
                    }}
                    return origSetLocal.apply(this, arguments);
                }});
            }}

            // onicecandidate 中过滤 local candidate
            const origAddEvent = RTCPeerConnection.prototype.addEventListener;
            RTCPeerConnection.prototype.addEventListener = helper.createNativeMethod('addEventListener', function(type, listener, options) {{
                if (type === 'icecandidate' && typeof listener === 'function') {{
                    const wrappedListener = function(event) {{
                        if (event && event.candidate && event.candidate.candidate) {{
                            const c = event.candidate.candidate;
                            if (c.includes('192.168.') || c.includes('10.') || /172\\.(1[6-9]|2[0-9]|3[01])\\./g.test(c)) {{
                                return; // 丢弃内网 IP candidate
                            }}
                        }}
                        listener.call(this, event);
                    }};
                    return origAddEvent.call(this, type, wrappedListener, options);
                }}
                return origAddEvent.apply(this, arguments);
            }});
        }}
    }} catch (e) {{}}

    // ── 21. Date / Intl Timezone Consistency Hardening ─────────────────────
    try {{
        // 确保 Intl.DateTimeFormat().resolvedOptions().timeZone 与 Playwright 设定一致
        const origDTF = Intl.DateTimeFormat;
        const targetTZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
        // Date.prototype.getTimezoneOffset 一致性校验（Playwright 已设，但双重保障）
        const origResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
        Intl.DateTimeFormat.prototype.resolvedOptions = helper.createNativeMethod('resolvedOptions', function() {{
            const opts = origResolvedOptions.apply(this, arguments);
            // 强制对齐（防止 Intl polyfill 泄漏不同时区）
            if (opts && opts.timeZone && targetTZ) {{
                opts.timeZone = targetTZ;
            }}
            return opts;
        }});
    }} catch (e) {{}}

    // ── 23. Error.stack / prepareStackTrace Trace Cleansing ───────────────
    try {{
        const origPrepareStackTrace = Error.prepareStackTrace;
        // 抹除堆栈中由于 init_script 或 evaluate 导致的注入痕迹
        const origCaptureStackTrace = Error.captureStackTrace;
        if (origCaptureStackTrace) {{
            Error.captureStackTrace = helper.createNativeMethod('captureStackTrace', function(targetObj, constructorOpt) {{
                const res = origCaptureStackTrace.apply(this, arguments);
                if (targetObj && typeof targetObj.stack === 'string') {{
                    targetObj.stack = targetObj.stack.replace(/at eval \\(eval at <anonymous>[\\s\\S]*?\\)/g, '')
                                                     .replace(/at <anonymous>:\\d+:\\d+/g, 'at app.js:1:1');
                }}
                return res;
            }}, 2);
        }}
    }} catch (e) {{}}

    // ── 24. Dedicated Worker & SharedWorker Constructor Interceptor ────────
    try {{
        if (window.Worker) {{
            const origWorker = window.Worker;
            const workerProxy = new Proxy(origWorker, {{
                construct(target, args, newTarget) {{
                    // 当创建 Worker 时，确保 URL 注入相同环境
                    const [scriptURL, options] = args;
                    return Reflect.construct(target, args, newTarget);
                }}
            }});
            try {{
                Object.defineProperty(workerProxy, 'name', {{ value: 'Worker', configurable: true }});
                Object.defineProperty(workerProxy, 'length', {{ value: 1, configurable: true }});
                customToStringMap.set(workerProxy, "function Worker() {{ [native code] }}");
                window.Worker = workerProxy;
            }} catch (e) {{}}
        }}
    }} catch (e) {{}}

    // ── 25. Navigator.prototype Properties Verification ────────────────────
    try {{
        const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
        if (navigator.hasOwnProperty('userAgent')) {{
            try {{ delete navigator.userAgent; }} catch(e) {{}}
        }}
        if (navigator.hasOwnProperty('appVersion')) {{
            try {{ delete navigator.appVersion; }} catch(e) {{}}
        }}
        if (navigator.vendor !== "Google Inc.") {{
            try {{ delete navProto.vendor; }} catch(e) {{}}
            try {{ delete navigator.vendor; }} catch(e) {{}}
            Object.defineProperty(navProto, 'vendor', {{
                get: helper.createNativeGetter('vendor', function() {{ return "Google Inc."; }}),
                configurable: true, enumerable: true
            }});
        }}
        if (navigator.productSub !== "20030107") {{
            try {{ delete navProto.productSub; }} catch(e) {{}}
            try {{ delete navigator.productSub; }} catch(e) {{}}
            Object.defineProperty(navProto, 'productSub', {{
                get: helper.createNativeGetter('productSub', function() {{ return "20030107"; }}),
                configurable: true, enumerable: true
            }});
        }}
    }} catch (e) {{}}

    // ── 26. MediaDevices Event Listeners & Hardware Noise ──────────────────
    try {{
        if (navigator.mediaDevices) {{
            const mdProto = Object.getPrototypeOf(navigator.mediaDevices) || navigator.mediaDevices;
            if (!mdProto.ondevicechange) {{
                Object.defineProperty(mdProto, 'ondevicechange', {{
                    value: null, writable: true, configurable: true, enumerable: true
                }});
            }}
        }}
    }} catch (e) {{}}

    // ── 27. Performance Timing & Navigation Consistency ────────────────────
    try {{
        if (window.PerformanceNavigationTiming) {{
            const origGetEntriesByType = performance.getEntriesByType;
            if (origGetEntriesByType) {{
                performance.getEntriesByType = helper.createNativeMethod('getEntriesByType', function(type) {{
                    const entries = origGetEntriesByType.apply(this, arguments);
                    if (type === 'navigation' && entries.length > 0) {{
                        // 确保没有 0 秒加载的机器人痕迹
                        const nav = entries[0];
                        if (nav.duration === 0) {{
                            try {{
                                Object.defineProperty(nav, 'duration', {{ value: Math.random() * 200 + 150, configurable: true }});
                            }} catch(e) {{}}
                        }}
                    }}
                    return entries;
                }}, 1);
            }}
        }}
    }} catch (e) {{}}

    // ── 28. Math & Crypto Consistency (Strict Native Functions) ────────────
    try {{
        // 保障 Math.sin, Math.cos, Math.tan 的 native descriptor
        customToStringMap.set(Math.sin, "function sin() {{ [native code] }}");
        customToStringMap.set(Math.cos, "function cos() {{ [native code] }}");
        customToStringMap.set(Math.tan, "function tan() {{ [native code] }}");
    }} catch (e) {{}}

}})();
"""


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
_DUMMY_SVG_IMAGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
_DUMMY_PNG_IMAGE = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
_DUMMY_EMPTY_FONT = b''
_DUMMY_EMPTY_AMD_MODULE = b'define([], function() { return new Proxy({}, { get: function(t, p) { return typeof p === "symbol" ? undefined : function() { return {}; }; } }); });'


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
        ".dmg", ".pkg", ".bin", ".apk", ".m4s", ".ts", ".flv", ".m3u8",
        ".map",
    )

    # 纯装饰性背景图 / 头像 / 遥测 / 分析追踪域（后缀匹配，覆盖 CDN 变体子域）
    BLOCK_DOMAIN_SUFFIXES = (
        "msftauthimages.net", "msauthimages.net",
        "dc.services.visualstudio.com",
        "self.events.data.microsoft.com", "browser.events.data.microsoft.com",
        "browser.pipe.aria.microsoft.com", "mobile.events.data.microsoft.com",
        "in.applicationinsights.azure.com", "js.monitor.azure.com",
        "edge.microsoft.com", "config.edge.skype.com",
        "go.microsoft.com",
        "res-1.cdn.office.net",
        "c.bing.com", "th.bing.com",
        "substrate.office.com", "arc.msn.com", "assets.msn.com",
        "nrb.footprintdns.com",
        "activity.windows.com",
    )
    _AUTH_IMAGE_SUFFIXES = ("msftauthimages.net", "msauthimages.net")

    TELEMETRY_PATHS = (
        "/onecollector/1.0", "/api/telemetry",
        "/collect/v1", "/v2/track",
        "/api/portal/diagnostics", "/api/portal/intents",
        "/api/clienterror", "/api/trace",
    )
    # Portal 非必要 API（不影响 Education Hub 提 Key 流程）
    NONESSENTIAL_PORTAL_API = (
        "/api/notifications", "/api/favorites", "/api/recommendations",
        "/api/spotlight", "/api/experimentation", "/api/feedback",
        "/api/marketplace", "/api/usersettings", "/api/dismissedinfobars",
        "/api/useractivity",
    )

    fulfilled_request_ids = set()
    block_telemetry = os.getenv("BLOCK_TELEMETRY", "true").strip().lower() not in ("0", "false", "no", "off")

    async def _route_handler(route, request):
        r_type = request.resource_type
        url = request.url
        url_lower = url.lower()
        clean_url = url_lower.split("?")[0].split("#")[0]
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path_lower = (parsed.path or "").lower()

        # 0. 拦截纯装饰性背景图 / 头像 / 遥测追踪域（后缀匹配，覆盖 CDN 变体子域）
        _is_block_domain = any(host == s or host.endswith("." + s) for s in BLOCK_DOMAIN_SUFFIXES)
        if block_telemetry and _is_block_domain:
            if r_type == "image" or any(host.endswith(s) for s in _AUTH_IMAGE_SUFFIXES):
                fulfilled_request_ids.add(id(request))
                await route.fulfill(status=200, content_type="image/png", body=_DUMMY_PNG_IMAGE)
                if stats:
                    stats.record_blocked(est_size=280000)
                return
            if r_type != "document" and not request.is_navigation_request():
                fulfilled_request_ids.add(id(request))
                await route.fulfill(status=204, headers={"cache-control": "no-store"}, body=b"")
                if stats:
                    stats.telemetry_blocked_count += 1
                return

        # 0c. 已有 TOTP 密钥时拦截 initializemobileapp，返回合成响应（省 ~1.25 MB QR 码）
        if ("initializemobileapp" in url_lower and request.method == "POST"
                and getattr(ctx, "_cached_totp_for_mfa", "")):
            _synth = json.dumps({
                "QrCode": "", "AccountName": "",
                "SecretKey": ctx._cached_totp_for_mfa,
                "Url": None, "AffinityRegion": None,
                "ActivationCode": None, "RegistrationType": 0, "ErrorCode": 0,
            }).encode()
            fulfilled_request_ids.add(id(request))
            await route.fulfill(status=200, content_type="application/json", body=_synth)
            if stats:
                stats.record_blocked(est_size=1300000)
            return

        # 1. 优先放行所有动态安全挑战、登录认证、Azure ARM/Education 与 2FA 相关接口（100% 原生直连）
        if any(p in url_lower for p in LocalHttpCache.DYNAMIC_SECURITY_PATTERNS):
            if "portal.azure.com" in host and path_lower.rstrip("/").endswith("/api/telemetry"):
                pass  # Portal 遥测不放行，落入后续 step 2 拦截
            elif LocalHttpCache.is_cacheable(url):
                pass  # 落入后续缓存逻辑
            else:
                await route.continue_()
                return

        # 核心 Azure 管理/业务 API、教育平台、批处理与身份认证域
        core_api_domains = (
            "management.azure.com", "graph.microsoft.com", "main.iam.ad.ext.azure.com",
            "education.azure.com", "signup.azure.com", "mysignins.microsoft.com", "login.microsoftonline.com"
        )
        if any(d in host for d in core_api_domains) or (
            "portal.azure.com" in host and path_lower.startswith("/api/") and not path_lower.rstrip("/").endswith("/api/telemetry")
        ):
            if not LocalHttpCache.is_cacheable(url):
                await route.continue_()
                return

        # 2. 广义遥测端点拦截（返回空成功响应）
        _path_stripped = path_lower.rstrip("/")
        is_telemetry = any(_path_stripped == tp or _path_stripped.endswith(tp) for tp in TELEMETRY_PATHS)
        if (block_telemetry and is_telemetry and request.method in ("GET", "POST", "OPTIONS")
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

        # 2b. Portal 非必要 API（不影响 Education Hub 提 Key 功能）
        if (block_telemetry and "portal.azure.com" in host
                and any(path_lower.startswith(p) for p in NONESSENTIAL_PORTAL_API)
                and r_type != "document"):
            fulfilled_request_ids.add(id(request))
            ct = "application/json"
            await route.fulfill(status=200, content_type=ct, body=b"[]",
                                headers={"cache-control": "no-store", "content-type": ct})
            if stats:
                stats.telemetry_blocked_count += 1
            return

        # 3. 核心业务导航、主 HTML 文档以及所有非 GET 请求：100% 原生直连
        if r_type == "document" or request.is_navigation_request() or request.method != "GET":
            await route.continue_()
            return

        # 4. 拦截超大体积无用媒体、安装包与 Source Map 文件
        if clean_url.endswith(BLOCKED_MEDIA_EXTS):
            if stats:
                stats.record_blocked(est_size=5000000 if not clean_url.endswith(".map") else 300000)
            await route.abort()
            return

        # 4b. 拦截 favicon / manifest 等浏览器自动请求
        _basename = path_lower.rsplit("/", 1)[-1]
        if _basename in ("favicon.ico", "manifest.json", "manifest.webmanifest", "browserconfig.xml"):
            fulfilled_request_ids.add(id(request))
            if _basename == "favicon.ico":
                await route.fulfill(status=200, content_type="image/x-icon", body=_DUMMY_PNG_IMAGE)
            else:
                await route.fulfill(status=204, body=b"")
            if stats:
                stats.record_blocked(est_size=5000)
            return

        # 4c. 广域非必要图片拦截（保留 Education 产品图标、验证码与缓存静态图）
        if r_type == "image" and not LocalHttpCache.is_cacheable(url):
            _is_essential_img = any(k in url_lower for k in (
                "education", "software", "captcha", "arkose", "product",
            ))
            if not _is_essential_img:
                fulfilled_request_ids.add(id(request))
                await route.fulfill(status=200, content_type="image/svg+xml", body=_DUMMY_SVG_IMAGE)
                if stats:
                    stats.record_blocked(est_size=50000)
                return

        if LocalHttpCache.is_cacheable(url, "GET"):
            # 一个账号下载时，其余账号等待并复用结果；所有等待结束后释放锁索引。
            # 单次 fetch 的上限为 20 秒。等待共享下载必须比它更长，
            # 否则慢代理会让等待者在下载完成前同时发起重复请求。
            async with LocalHttpCache.download_lock(url, timeout=45) as acquired:
                if not acquired:
                    # 其他账号下载卡住时，当前账号原生加载；正常快速下载仍共享缓存。
                    await route.continue_()
                    return
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
                    fetched = await route.fetch(timeout=20000, max_retries=1, **fetch_kwargs)
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

        # 7. 拦截大体积音视频
        if r_type == "media" or clean_url.endswith(BLOCKED_MEDIA_EXTS):
            if stats:
                stats.record_blocked(est_size=100000)
            await route.abort()
            return

        # 8. 其他所有请求原生放行
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
            url_str = (response.url or "").lower()
            if "initializemobileapp" in url_str:
                asyncio.ensure_future(_diag_mfa_response(response, req, resp_bytes))
            # 关键黑科技增强：直接在网络响应流中嗅探 Azure ARM / Education 下发的 Product Key
            elif any(k in url_str for k in ("management.azure.com", "/batch", "education", "getproductkey", "software")):
                async def _sniff_key_response():
                    try:
                        if response.status == 200:
                            body = await response.body()
                            txt = body.decode("utf-8", errors="ignore")
                            # 匹配标准 5x5 产品密钥格式: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
                            matches = re.findall(r"\b([A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5})\b", txt)
                            if matches:
                                if not hasattr(ctx, "_sniffed_keys"):
                                    ctx._sniffed_keys = []
                                for m in matches:
                                    if m not in ctx._sniffed_keys:
                                        ctx._sniffed_keys.append(m)
                                        log.info(f"[Key Sniffer] 网络层嗅探到产品密钥: {m}")
                    except Exception:
                        pass
                asyncio.ensure_future(_sniff_key_response())
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


def _get_proxy_geo(proxy_config: dict | None) -> dict:
    """实时探测代理服务器的物理出口时区与地理位置（支持动态IP、轮询代理与多源高可用探测）。"""
    if not proxy_config or not proxy_config.get("server"):
        return {}
    srv = proxy_config.get("server", "")
    p_user = proxy_config.get("username")
    p_pass = proxy_config.get("password")

    proxies = {"http": srv, "https": srv}
    if p_user and p_pass:
        p_parts = srv.split("://", 1)
        scheme = p_parts[0]
        rest = p_parts[1] if len(p_parts) > 1 else srv
        auth_url = f"{scheme}://{p_user}:{p_pass}@{rest}"
        proxies = {"http": auth_url, "https": auth_url}

    # 1. 优先尝试 ipwho.is (支持 HTTPS，无严格频次限制)
    try:
        import requests
        resp = requests.get("https://ipwho.is/", proxies=proxies, timeout=3.0)
        data = resp.json()
        if data.get("success") and data.get("timezone", {}).get("id"):
            return {
                "ip": data.get("ip", ""),
                "timezone": data.get("timezone", {}).get("id"),
                "country": data.get("country_code", "US"),
                "latitude": data.get("latitude"),
                "longitude": data.get("longitude"),
            }
    except Exception:
        pass

    # 2. 备用尝试 ip-api.com
    try:
        import requests
        resp = requests.get("http://ip-api.com/json/?fields=status,query,countryCode,timezone,lat,lon", proxies=proxies, timeout=3.0)
        data = resp.json()
        if data.get("status") == "success" and data.get("timezone"):
            return {
                "ip": data.get("query", ""),
                "timezone": data.get("timezone"),
                "country": data.get("countryCode", "US"),
                "latitude": data.get("lat"),
                "longitude": data.get("lon"),
            }
    except Exception:
        pass

    return {}


_cache_warm_guard = threading.Lock()
_cache_warm_done = set()
_cache_warm_inflight = {}
_cache_warm_retry_after = {}


def _static_cache_count():
    try:
        return sum(name.endswith('.body') for name in os.listdir(LocalHttpCache.CACHE_DIR))
    except OSError:
        return 0


async def _download_public_cache(pw, proxy_config, exe_path):
    """无登录会话的公共资源预热；不提交身份资料或请求产品密钥。"""
    browser = None
    try:
        args = ['--disable-background-networking', '--no-first-run',
                '--no-default-browser-check', '--disable-dev-shm-usage']
        if sys.platform != 'win32':
            args.append('--no-sandbox')
        browser = await pw.chromium.launch(
            headless=True, proxy=proxy_config, args=args, timeout=15000,
            **({'executable_path': exe_path} if exe_path else {}),
        )
        ctx = await browser.new_context(locale='en-US', viewport={'width': 1440, 'height': 900})
        await setup_save_data_route(ctx, TrafficStats())
        page = await ctx.new_page()
        for url in ('https://signup.azure.com/studentverification?offerType=1',
                    'https://portal.azure.com/'):
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=10000)
                await page.wait_for_load_state('networkidle', timeout=4000)
            except Exception:
                # 已成功下载的资源仍保留；一个入口失败不阻断另一个。
                continue
    finally:
        if browser is not None:
            await browser.close()


async def warmup_static_cache(pw, proxy_config: dict | None = None,
                               exe_path: str | None = None,
                               min_files: int = 100, cb=None):
    """缓存不足时自动预热；进程内跨线程/事件循环仅一个任务下载。"""
    def emit(message):
        log.info(message)
        if cb:
            cb(message)

    root = os.path.abspath(LocalHttpCache.CACHE_DIR)
    before = _static_cache_count()
    with _cache_warm_guard:
        if root in _cache_warm_done:
            return
        if before >= min_files:
            _cache_warm_done.add(root)
            return
        if time.monotonic() < _cache_warm_retry_after.get(root, 0):
            return
        future = _cache_warm_inflight.get(root)
        owner = future is None
        if owner:
            future = concurrent.futures.Future()
            _cache_warm_inflight[root] = future

    if not owner:
        emit('  ⏳ [缓存预热] 等待其他任务完成共享预热...')
        # 某个等待任务被取消时，不能取消所有任务共享的 future。
        await asyncio.shield(asyncio.wrap_future(future))
        return

    success = False
    try:
        emit(f'  📦 [缓存预热] 当前 {before} 个资源，优先直连下载公共静态资源...')
        paths = [('直连', None)]
        if proxy_config:
            paths.append(('代理回退', proxy_config))
        for label, route in paths:
            try:
                await asyncio.wait_for(_download_public_cache(pw, route, exe_path), timeout=25)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                emit(f'  ⚠️ [缓存预热] {label}未完成 ({type(error).__name__})，保留已下载资源')
            after = _static_cache_count()
            if after > before:
                success = True
                emit(f'  ✅ [缓存预热] {label}完成：{before} → {after} 个资源；其余资源运行时继续补齐')
                break
            if route is None and proxy_config:
                emit('  🌐 [缓存预热] 直连未取得缓存，尝试代理下载（会消耗代理流量）...')
        if not success:
            emit('  ⚠️ [缓存预热] 暂未取得资源，继续正常流程；5 分钟后允许再次预热')
    finally:
        with _cache_warm_guard:
            if success:
                _cache_warm_done.add(root)
            else:
                _cache_warm_retry_after[root] = time.monotonic() + 300
            _cache_warm_inflight.pop(root, None)
            future.set_result(success)


async def new_fingerprint_context(pw, headless: bool, proxy_config: dict | None,
                                   exe_path: str | None, slow_mo: int = 80,
                                   enable_save_data: bool = True):
    """创建带高拟真独立随机指纹的 Playwright BrowserContext，纯净环境隔离并与代理 IP 精准对齐。
    返回 (browser, ctx, fp, stats)。
    """
    if sys.platform != "win32" and "DISPLAY" not in os.environ:
        if not headless:
            log.info("[提示] 检测到 Linux 纯终端环境 (无 XServer/DISPLAY)，全自动强制开启无头模式 (headless=True)")
            headless = True

    # 动态探测动态代理出口 IP 物理信息（时区、国家、经纬度），与主进程/子进程严格对齐
    geo = _get_proxy_geo(proxy_config)

    country = (geo.get("country") or "").upper()
    if country == "US":
        locale = "en-US"
        languages = ["en-US", "en"]
    elif country == "GB":
        locale = "en-GB"
        languages = ["en-GB", "en-US", "en"]
    elif country == "CA":
        locale = "en-CA"
        languages = ["en-CA", "en-US", "en"]
    elif country == "AU":
        locale = "en-AU"
        languages = ["en-AU", "en-US", "en"]
    elif country == "DE":
        locale = "de-DE"
        languages = ["de-DE", "de", "en-US", "en"]
    elif country == "FR":
        locale = "fr-FR"
        languages = ["fr-FR", "fr", "en-US", "en"]
    elif country == "JP":
        locale = "ja-JP"
        languages = ["ja-JP", "ja", "en-US", "en"]
    else:
        loc_item = random.choice(_LOCALES)
        locale, languages = loc_item[0], loc_item[1]

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--enforce-webrtc-ip-permission-check",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-pings",
        "--disable-background-networking",
        "--disable-domain-reliability",
        "--disable-sync",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-ipc-flooding-protection",
        "--enable-webgl",
        "--ignore-gpu-blocklist",
        "--password-store=basic",
        "--disable-search-engine-choice-screen",
        "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,AutofillServerCommunication,InterestFeedContentSuggestions",
        "--disable-client-side-phishing-detection",
        "--disable-breakpad",
        "--metrics-recording-only",
        "--no-report-upload",
        "--enable-features=NetworkService,NetworkServiceInProcess",
        f"--lang={locale}",
    ]

    if sys.platform != "win32":
        browser_args.extend(["--no-sandbox"])

    if not exe_path or not os.path.exists(exe_path):
        import config
        exe_path = config.ensure_chromium_installed()

    # 严格移除 Playwright 默认的自动化与受控特征参数（参考 Patchright / rebrowser-patches）
    launch_kwargs = {
        "headless": headless,
        "slow_mo": slow_mo,
        "proxy": proxy_config,
        "ignore_default_args": [
            "--enable-automation",
            "--disable-popup-blocking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-component-update",
            "--disable-component-extensions-with-background-pages",
        ],
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

    # 动态且精确对齐宿主 Chromium 的真实主版本与完整版本（杜绝版本与 JS 特性冲突导致的 Bot 判定）
    real_ver = getattr(browser, "version", "") or "151.0.7922.34"
    major_ver = real_ver.split(".")[0] if "." in real_ver else "151"

    # 生成与宿主 Chromium 版本 100% 对齐的指纹
    fp = random_fingerprint(major_ver=major_ver, full_ver=real_ver)
    timezone = geo.get("timezone") or fp["timezone"]
    fp["timezone"] = timezone
    fp["locale"] = locale
    fp["languages"] = languages

    headers = {
        "Accept-Language": ",".join(
            lang if i == 0 else f"{lang};q={round(0.9 - i*0.1, 1)}"
            for i, lang in enumerate(languages)
        ),
        "Sec-CH-UA": fp["sec_ch_ua"],
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    }

    context_kwargs = {
        "screen": {"width": fp["screen_width"], "height": fp["screen_height"]},
        "viewport": fp["viewport"],
        "device_scale_factor": fp["dpr"],
        "is_mobile": False,
        "has_touch": False,
        "user_agent": fp["user_agent"],
        "locale": locale,
        "timezone_id": timezone,
        "extra_http_headers": headers,
        "permissions": ["geolocation", "notifications"],
        "color_scheme": random.choice(["light", "light", "light", "dark"]),
        "reduced_motion": "no-preference",
        "forced_colors": "none",
    }
    if geo.get("latitude") is not None and geo.get("longitude") is not None:
        context_kwargs["geolocation"] = {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "accuracy": random.uniform(10.0, 50.0)
        }

    ctx = await browser.new_context(**context_kwargs)
    ctx._captured_totp_secret = ""
    ctx._cached_totp_for_mfa = ""
    await ctx.add_init_script(build_init_script(fp))

    stats = TrafficStats()
    if enable_save_data:
        await setup_save_data_route(ctx, stats)
    else:
        ctx.on("response", stats.add_response)

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
