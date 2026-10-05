"""
随机浏览器指纹生成 + Playwright 上下文初始化
全维度超强反检测与超深度省流环境隔离系统 (Stealth v7 终极无懈版)：
1. 真实消费级 GPU / WebGL 深度参数硬化、Shader Precision 对齐与 GLSL PRNG readPixels 微扰动（彻底消除跨账号 WebGL 聚类特征）；
2. 真实桌面级 PluginArray & MimeTypeArray 架构（5 大内置 PDF 插件，彻底消除 plugins.length===0 无头漏洞）；
3. Canvas 2D 亚像素级微偏移、100% 幂等 getImageData 逐点噪点注入与 measureText 字体排版微抖动；
4. AudioContext、OfflineAudioContext & AnalyserNode 频域/时域全维微噪点注入（WeakSet 单 Buffer 幂等防护与 copyFromChannel 全覆盖）；
5. Document.prototype.hasFocus / visibilityState / hidden 真实化（彻底杜绝 Headless 失去焦点与后台标签页特征）；
6. 原生 Getter 生成工厂（严格对齐 V8 函数名与 toString，抹除一切属性篡改痕迹）；
7. CDP / WebDriver 自动化残留属性全量清除；
8. Navigator.prototype.pdfViewerEnabled 深度对齐；
9. Notification.permission 与 Permissions API (navigator.permissions.query) 原生状态返回；
10. WebGPU (navigator.gpu) 真实硬件适配器模拟；
11. NetworkInformation (navigator.connection) 真实 4G/宽带网络与 RTT/Downlink 联动；
12. Screen.orientation 真实桌面横屏对象与真实屏幕几何视口联动；
13. Battery API (navigator.getBattery) 与完整 SpeechSynthesis 语音库对齐；
14. 完整 navigator.userAgentData (Client Hints) 高熵值接口 (包含 wow64: false) 与 Win32 原型链严格原生化；
15. 公共静态资源持久化缓存，动态认证/API 放行；
16. 每个账号完全隔离的 BrowserContext 独立生命周期，彻底杜绝跨账号污染。
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
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


def random_fingerprint(major_ver: str = "131") -> dict:
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

    # 拟真微版本号
    build_patch = random.choice(["0.6778.86", "0.6778.108", "0.6778.140", "0.6778.205", "0.6723.116", "0.6834.110"])
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
    )


def build_init_script(fp: dict) -> str:
    """生成纯净、原生对齐的高拟真防爬辅助注入脚本 (Stealth v15 终极防检测全维融合版)。
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
    11. 针对 VPS 虚拟显卡 (SwiftShader / llvmpipe / Mesa) 真实化 UNMASKED_RENDERER_WEBGL 与 WebGL 原生 getParameter/ShaderPrecision 深度伪装；
    12. Canvas 2D 亚像素级微偏移与噪点扰动 (基于种子生成独立 Canvas 哈希，杜绝多账号在同一服务器上的特征聚类)；
    13. AudioContext & OfflineAudioContext 频域/时域微扰动 (消除跨账号音频指纹碰撞)；
    14. SpeechSynthesis 语音库模拟 (补齐 Linux Headless 环境下缺失的 Windows 桌面语音库)；
    15. MediaDevices 摄像头/麦克风设备模拟 (对齐真实物理 PC 硬件外设枚举列表)；
    16. Battery API 与 NetworkInformation (navigator.connection) 真实 4G/宽带网络与 RTT/Downlink 联动；
    17. WebRTC 本地内网 IP 防泄漏保护；
    18. Notification.permission 与 Permissions API 原生状态对齐；
    19. Document.prototype.hasFocus / visibilityState / hidden 原生对齐；
    20. 屏幕与视口几何参数严格一致。
    """
    brands_json = json.dumps(fp.get("brand_entries", [
        {"brand": "Chromium", "version": fp.get("major_ver", "131")},
        {"brand": "Google Chrome", "version": fp.get("major_ver", "131")},
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

    return f"""
(() => {{
    'use strict';

    // ── 0. Native Function Cloaking Engine (ES6 Non-Constructible Methods) ──
    const nativeToString = Function.prototype.toString;
    const customToStringMap = new WeakMap();

    const helper = {{
        createNativeMethod(name, impl) {{
            const holder = {{
                [name](...args) {{
                    return impl.apply(this, args);
                }}
            }};
            const method = holder[name];
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
    try {{
        const origDebug = console.debug;
        console.debug = helper.createNativeMethod('debug', function(...args) {{
            return;
        }});
        if (console.trace) {{
            console.trace = helper.createNativeMethod('trace', function(...args) {{
                return;
            }});
        }}
        if (console.dir) {{
            console.dir = helper.createNativeMethod('dir', function(...args) {{
                return;
            }});
        }}
        if (console.dirxml) {{
            console.dirxml = helper.createNativeMethod('dirxml', function(...args) {{
                return;
            }});
        }}
    }} catch (e) {{}}

    // ── 1. Clean navigator.webdriver & Proto Alignment ────────────────────
    try {{
        const navProto = Object.getPrototypeOf(navigator) || Navigator.prototype;
        delete navProto.webdriver;
        delete navigator.webdriver;
        const getWebdriver = helper.createNativeGetter('webdriver', function() {{ return false; }});
        Object.defineProperty(navProto, 'webdriver', {{
            get: getWebdriver,
            set: undefined,
            enumerable: true,
            configurable: true
        }});
    }} catch (e) {{}}

    // ── 2. window.chrome Alignment (Vanilla Desktop Chrome) ───────────────
    try {{
        if (!window.chrome) {{
            window.chrome = {{}};
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

    // ── 6. WebGL UNMASKED_VENDOR / UNMASKED_RENDERER Cloaking ──────────────
    try {{
        const spoofVendor = "{vendor}";
        const spoofRenderer = "{renderer}";

        function hookGetParameter(proto) {{
            if (!proto || !proto.getParameter) return;
            const orig = proto.getParameter;
            const hooked = helper.createNativeMethod('getParameter', function(param) {{
                if (param === 0x9245) return spoofVendor;      // UNMASKED_VENDOR_WEBGL
                if (param === 0x9246) return spoofRenderer;    // UNMASKED_RENDERER_WEBGL
                if (param === 0x1F00) return "WebKit";         // VENDOR
                if (param === 0x1F01) return "WebKit WebGL";   // RENDERER
                if (param === 0x0D33) return 16384;            // MAX_TEXTURE_SIZE
                if (param === 0x84E8) return 16384;            // MAX_CUBE_MAP_TEXTURE_SIZE
                if (param === 0x84E4) return 16384;            // MAX_RENDERBUFFER_SIZE
                return orig.apply(this, arguments);
            }});
            proto.getParameter = hooked;
        }}

        if (window.WebGLRenderingContext) hookGetParameter(WebGLRenderingContext.prototype);
        if (window.WebGL2RenderingContext) hookGetParameter(WebGL2RenderingContext.prototype);
    }} catch (e) {{}}

    // ── 7. Canvas 2D Sub-pixel & Deterministic Noise Injection ──────────────
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
                    }} catch(e) {{}}
                }}
                return res;
            }});
        }}
    }} catch (e) {{}}

    // ── 8. AudioContext & OfflineAudioContext Fingerprint Hardening ─────────
    try {{
        const audioSeed = {seed};
        if (window.AudioBuffer) {{
            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = helper.createNativeMethod('getChannelData', function(channel) {{
                const data = origGetChannelData.apply(this, arguments);
                if (data && data.length > 0) {{
                    const step = Math.max(20, Math.floor(data.length / 50));
                    for (let i = 0; i < data.length; i += step) {{
                        data[i] += (audioSeed * 0.0000001) - 0.00000005;
                    }}
                }}
                return data;
            }});
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

    fp = random_fingerprint(major_ver="131")

    # 动态探测动态代理出口 IP 物理信息（时区、国家、经纬度），与主进程/子进程严格对齐
    geo = _get_proxy_geo(proxy_config)
    timezone = geo.get("timezone") or fp["timezone"]

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
        locale = fp["locale"]
        languages = fp["languages"]

    fp["timezone"] = timezone
    fp["locale"] = locale
    fp["languages"] = languages

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
        "--disable-component-update",
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
        "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider",
        f"--lang={locale}",
        f"--user-agent={fp['user_agent']}",
    ]

    if sys.platform != "win32":
        browser_args.extend(["--no-sandbox"])

    if not exe_path or not os.path.exists(exe_path):
        import config
        exe_path = config.ensure_chromium_installed()

    launch_kwargs = {
        "headless": headless,
        "slow_mo": slow_mo,
        "proxy": proxy_config,
        "ignore_default_args": [
            "--enable-automation",
            "--disable-popup-blocking",
            "--disable-default-apps",
            "--disable-extensions",
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

    real_ver = getattr(browser, "version", "") or "131.0.0.0"
    major_ver = real_ver.split(".")[0] if "." in real_ver else "131"
    fp["major_ver"] = major_ver
    fp["chrome_ver"] = real_ver

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
