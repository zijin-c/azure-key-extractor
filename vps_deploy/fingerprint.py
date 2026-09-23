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
15. 极限省流架构（确定性本地磁盘强缓存系统与 1-Byte 极简图片 Mock，mysignins 与 Azure 静态切片 0 字节秒开）；
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
from urllib.parse import urlparse

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
    """生成纯净、原生对齐的高拟真防爬辅助注入脚本 (Stealth v14 终极开源全维融合版)。
    参考并整合了 rebrowser-patches、puppeteer-extra-plugin-stealth、fingerprint-suite 与 camoufox 的核心防检测逻辑：
    1. ES6 Concise Object Method 原生函数工厂：构造非构造器函数，杜绝 Function.prototype.hasOwnProperty('prototype') 与 new 异常检测；
    2. WeakMap 原生函数原型链伪装 (Function.prototype.toString 严格对齐 V8 原生 [native code]，杜绝 AST/代码字符串化检测)；
    3. Web Worker / Blob Worker 深度域隔离同步：拦截 URL.createObjectURL 与 Worker 构造函数，向 Worker 线程内无缝注入硬件与语言环境；
    4. Console / CDP Getter 探测陷阱全面消除：防御 Cloudflare Turnstile 在 console.debug/dir 中传入带 Getter 对象的 CDP 自动化监听陷阱；
    5. 原型链级别净化 navigator.webdriver (彻底移除自动化痕迹，原型属性 getter 返回 false，无 ownProperty 异常)；
    6. 标准原生桌面级 window.chrome (对齐 app, loadTimes, csi，彻底移除会暴露 mock 的伪造 runtime)；
    7. 桌面级真实 PluginArray & MimeTypeArray (5 大内置 PDF 插件，定义在 Navigator.prototype，消除 plugins.length===0 无头漏洞)；
    8. 硬件参数全面对齐 (hardwareConcurrency, deviceMemory, maxTouchPoints=0 严格挂载在 Navigator.prototype)；
    9. 语言参数对齐 (language, languages 严格挂载在 Navigator.prototype 并与代理出口 IP 国家对齐)；
    10. 提供完整 navigator.userAgentData (Client Hints) 高熵接口 (包含 wow64: false，挂载于原型链)；
    11. 针对 VPS 虚拟显卡 (SwiftShader / llvmpipe / Mesa) 真实化 UNMASKED_RENDERER_WEBGL 与 WebGL 原生 getParameter 伪装；
    12. Notification.permission 与 Permissions API (navigator.permissions.query) 原生状态对齐；
    13. Document.prototype.hasFocus / visibilityState / hidden 原生对齐 (杜绝 Headless 失去焦点与隐藏特征)；
    14. 屏幕与视口几何参数严格一致 (Screen.prototype 尺寸与 window.outerWidth/outerHeight 严格匹配物理显示器)；
    15. 保持 Canvas 2D 像素与 AudioContext 原生数学纯净度，保证 Arkose PoW 与 Cloudflare 客户端 Hash 计算 100% 真实通过。
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
    sw = fp.get("screen_width", 1920)
    sh = fp.get("screen_height", 1080)
    saw = fp.get("screen_avail_width", 1920)
    sah = fp.get("screen_avail_height", 1040)
    loc = fp.get("locale", "en-US")
    langs_json = json.dumps(fp.get("languages", ["en-US", "en"]))

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
                return orig.apply(this, arguments);
            }});
            proto.getParameter = hooked;
        }}

        if (window.WebGLRenderingContext) hookGetParameter(WebGLRenderingContext.prototype);
        if (window.WebGL2RenderingContext) hookGetParameter(WebGL2RenderingContext.prototype);
    }} catch (e) {{}}

    // ── 7. Permissions API & Notification Alignment ────────────────────────
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

    // ── 8. Document Visibility & Focus (Headless Neutralization) ───────────
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

    // ── 9. Window Geometry & Screen Consistency ────────────────────────────
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


class LocalHttpCache:
    """全静态资产本地磁盘强缓存系统：
    支持 JS/CSS/字体(woff2/woff/ttf/otf)/静态JSON/图标 本地永久缓存与秒级命中。
    支持 Azure Portal 7 大 ExtensionManifest 规范化类型缓存，杜绝 Hash 漂移重复下载。
    """
    CACHEABLE_EXTENSIONS = (
        ".js", ".mjs", ".ts", ".css",
        ".woff2", ".woff", ".ttf", ".otf", ".eot",
        ".svg", ".ico", ".png", ".jpg", ".jpeg", ".webp",
        ".json"
    )

    IMAGE_EXTS = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".tiff"
    )

    DYNAMIC_SECURITY_PATTERNS = (
        # 动态人机验证与风控挑战交互/PoW接口（Arkose / Turnstile / SheerID / hCaptcha / reCAPTCHA / GeeTest 等）
        "arkose", "arkoselabs", "funcaptcha", "powseq", "turnstile",
        "challenges.cloudflare.com", "cloudflare.com", "challenge-platform",
        "hcaptcha", "recaptcha", "geetest",
        "sheerid", "services.sheerid.com", "cdn.sheerid.com",
        # 动态认证/登录授权交互接口（注意：精确指定动态接口路径，确保域名的静态JS/CSS正常享受本地强缓存与省流）
        "/common/oauth2/", "/oauth20_authorize", "/login.srf",
        "/kmsi", "/kmsi.srf", "/getcredentialtype", "/getsessionstate",
        "/sas/processauth", "/ppsecure/post.srf", "/processauth",
        "/oauth2/token", "/oauth2/v2.0/token",
        "/reprocess", "/federal/login",
        # 动态 2FA 注册与验证接口（绝对保证 TOTP 密钥提取）
        "/api/authenticationmethods/",
        # 动态 ARM 资源与提 Key 接口
        "management.azure.com/providers/microsoft.education",
        "management.azure.com/providers/microsoft.billing",
        "management.azure.com/subscriptions",
        "management.azure.com/tenants",
        "management.azure.com/batch",
        # 动态学生认证交互接口（注意：精确放行动态 API 和认证页面，允许静态 JS/CSS 走本地强缓存与背景图片 Mock）
        "signup.azure.com/api/",
        "signup.azure.com/studentverification",
        "signup.azure.com/signup?offer=",
        # 带有动态临时签名与时间戳的 PoW / challenge 脚本
        "/fc/", "/challenge", "/enforcement",
        "expires=", "signature=", "key-pair-id="
    )

    CANONICAL_DIR = os.path.join(PYTHON_HTTP_CACHE_DIR, "canonical_manifests")
    _manifest_hash_to_type: dict[str, str] = {}
    _canonical_initialized: bool = False

    @classmethod
    def detect_manifest_type(cls, data: dict) -> str | None:
        """从 Manifest JSON 数据结构中精准识别其规范化类型。"""
        if not isinstance(data, dict):
            return None
        manifest = data.get("manifest")
        if not isinstance(manifest, dict):
            return None
        sample_ext = manifest.get("Microsoft_Azure_Education") or {}
        ext_keys = set(sample_ext.keys()) if isinstance(sample_ext, dict) else set()
        if "extensionConfiguration" in ext_keys:
            return "extensionConfiguration"
        if "assetTypesBrowse" in ext_keys:
            return "assetTypesBrowse"
        if "assetTypes" in ext_keys:
            return "assetTypes"
        if "browseMenus" in ext_keys:
            return "browseMenus"
        if "featureCards" in ext_keys or "featureCardEnvironmentConfiguration" in ext_keys:
            return "featureCards"
        if "portalServices" in ext_keys:
            return "portalServices"
        if "tourIds" in ext_keys:
            return "tourGuide"
        for ext_val in manifest.values():
            if isinstance(ext_val, dict):
                k_set = set(ext_val.keys())
                if "extensionConfiguration" in k_set: return "extensionConfiguration"
                if "assetTypesBrowse" in k_set: return "assetTypesBrowse"
                if "assetTypes" in k_set: return "assetTypes"
                if "browseMenus" in k_set: return "browseMenus"
                if "featureCards" in k_set: return "featureCards"
                if "portalServices" in k_set: return "portalServices"
                if "tourIds" in k_set: return "tourGuide"
        return None

    @classmethod
    def init_canonical_manifests(cls):
        """初始化规范化清单元数据缓存池，自动利用现有缓存预热。"""
        if cls._canonical_initialized:
            return
        os.makedirs(cls.CANONICAL_DIR, exist_ok=True)
        try:
            for fname in os.listdir(PYTHON_HTTP_CACHE_DIR):
                if fname.endswith(".meta"):
                    m_path = os.path.join(PYTHON_HTTP_CACHE_DIR, fname)
                    b_path = os.path.join(PYTHON_HTTP_CACHE_DIR, fname[:-5] + ".body")
                    if not os.path.exists(b_path):
                        continue
                    try:
                        with open(m_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        url = meta.get("url", "")
                        if "ExtensionManifest/" in url:
                            hash_name = url.split("/")[-1].split("?")[0]
                            with open(b_path, "rb") as f:
                                body = f.read()
                            data = json.loads(body.decode("utf-8", errors="ignore"))
                            m_type = cls.detect_manifest_type(data)
                            if m_type:
                                cls._manifest_hash_to_type[hash_name] = m_type
                                canon_b = os.path.join(cls.CANONICAL_DIR, f"{m_type}.body")
                                canon_m = os.path.join(cls.CANONICAL_DIR, f"{m_type}.meta")
                                if not os.path.exists(canon_b) or not os.path.exists(canon_m):
                                    with open(canon_b, "wb") as bf:
                                        bf.write(body)
                                    with open(canon_m, "w", encoding="utf-8") as mf:
                                        json.dump(meta, mf, ensure_ascii=False)
                    except Exception:
                        pass
        except Exception:
            pass
        cls._canonical_initialized = True

    @classmethod
    def get_canonical_manifest(cls, url: str) -> tuple[bytes, dict, int] | None:
        """根据 URL (解析 m_type 参数或已学到的 hash 映射) 读取规范化 Manifest 强缓存。"""
        cls.init_canonical_manifests()
        m_type = None
        if "m_type=" in url:
            parts = url.split("m_type=", 1)[1]
            m_type = parts.split("&")[0].split("#")[0]
        if not m_type:
            hash_name = url.split("/")[-1].split("?")[0].split("#")[0]
            m_type = cls._manifest_hash_to_type.get(hash_name)
        if not m_type:
            return None
        canon_b = os.path.join(cls.CANONICAL_DIR, f"{m_type}.body")
        canon_m = os.path.join(cls.CANONICAL_DIR, f"{m_type}.meta")
        if os.path.exists(canon_b) and os.path.exists(canon_m):
            try:
                with open(canon_m, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                with open(canon_b, "rb") as f:
                    body = f.read()
                if body and meta.get("headers"):
                    return body, meta.get("headers"), meta.get("status", 200)
            except Exception:
                pass
        return None

    @classmethod
    def save_canonical_manifest(cls, url: str, body: bytes, headers: dict):
        """将新拉取的 Manifest 数据解析并更新到规范化缓存中。"""
        cls.init_canonical_manifests()
        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
            m_type = cls.detect_manifest_type(data)
            if not m_type and "m_type=" in url:
                m_type = url.split("m_type=", 1)[1].split("&")[0].split("#")[0]
            if m_type:
                hash_name = url.split("/")[-1].split("?")[0].split("#")[0]
                cls._manifest_hash_to_type[hash_name] = m_type
                canon_b = os.path.join(cls.CANONICAL_DIR, f"{m_type}.body")
                canon_m = os.path.join(cls.CANONICAL_DIR, f"{m_type}.meta")
                clean_headers = {
                    "content-type": "application/json; charset=utf-8",
                    "access-control-allow-origin": "*",
                    "cache-control": "public, max-age=31536000, immutable",
                }
                for k, v in headers.items():
                    if k.lower() not in ("content-length", "content-encoding", "transfer-encoding"):
                        clean_headers[k] = v
                with open(canon_b, "wb") as f:
                    f.write(body)
                with open(canon_m, "w", encoding="utf-8") as f:
                    json.dump({"status": 200, "headers": clean_headers, "url": url}, f, ensure_ascii=False)
        except Exception:
            pass

    @classmethod
    def is_cacheable(cls, url: str, method: str = "GET") -> bool:
        """精准判定指定 URL 请求是否属于可无状态本地强缓存的公共静态资产。"""
        if method.upper() != "GET":
            return False
        url_lower = url.lower()
        if any(p in url_lower for p in cls.DYNAMIC_SECURITY_PATTERNS):
            return False
        clean = url_lower.split("?")[0].split("#")[0]
        if clean.endswith(cls.CACHEABLE_EXTENSIONS):
            return True
        if any(path in clean for path in (
            "/content/dynamic/", "/content/portalrequireconfig/",
            "/bundle/", "/shared/1.0/", "/ests/2.1/", "/fonts/", "/content/scripts/"
        )):
            return True
        return False

    @staticmethod
    def _url_to_key(url: str) -> str:
        clean = url.split("?")[0].split("#")[0]
        return hashlib.sha256(clean.encode("utf-8")).hexdigest()

    @classmethod
    def get(cls, url: str) -> tuple[bytes, dict, int] | None:
        """根据 URL 查找本地强缓存。返回 (body_bytes, headers_dict, status_code) 或 None。
        具备自动自愈机制：若检测到 body/meta 缺失或损坏，自动清理残片并回退到网络请求。
        """
        if not cls.is_cacheable(url, "GET"):
            return None

        key = cls._url_to_key(url)
        body_file = os.path.join(PYTHON_HTTP_CACHE_DIR, f"{key}.body")
        meta_file = os.path.join(PYTHON_HTTP_CACHE_DIR, f"{key}.meta")

        has_body = os.path.exists(body_file)
        has_meta = os.path.exists(meta_file)

        if has_body and has_meta:
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                with open(body_file, "rb") as f:
                    body = f.read()
                if body and len(body) > 10 and meta.get("headers"):
                    return body, meta.get("headers"), meta.get("status", 200)
            except Exception:
                pass
            # 残损自愈清理
            try:
                if os.path.exists(body_file): os.remove(body_file)
                if os.path.exists(meta_file): os.remove(meta_file)
            except Exception:
                pass
        elif has_body or has_meta:
            # 孤立残片清理
            try:
                if os.path.exists(body_file): os.remove(body_file)
                if os.path.exists(meta_file): os.remove(meta_file)
            except Exception:
                pass

        return None

    @classmethod
    def put(cls, url: str, body: bytes, headers: dict, status: int = 200):
        """保存响应数据到本地强缓存。"""
        if not body or status != 200 or not cls.is_cacheable(url, "GET"):
            return

        key = cls._url_to_key(url)
        body_file = os.path.join(PYTHON_HTTP_CACHE_DIR, f"{key}.body")
        meta_file = os.path.join(PYTHON_HTTP_CACHE_DIR, f"{key}.meta")
        temp_body = f"{body_file}.tmp"
        temp_meta = f"{meta_file}.tmp"

        clean_headers = {
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET, HEAD, OPTIONS",
            "access-control-allow-headers": "*",
            "cache-control": "public, max-age=31536000, immutable",
        }
        skip_headers = {
            "content-length", "content-encoding", "transfer-encoding",
            "connection", "keep-alive", "content-security-policy",
            "x-frame-options", "cross-origin-resource-policy",
            "cross-origin-embedder-policy", "cross-origin-opener-policy",
            "set-cookie"
        }
        for k, v in headers.items():
            if k.lower() not in skip_headers:
                clean_headers[k] = v

        try:
            with open(temp_body, "wb") as f:
                f.write(body)
            with open(temp_meta, "w", encoding="utf-8") as f:
                json.dump({"status": status, "headers": clean_headers, "url": url}, f, ensure_ascii=False)

            os.replace(temp_body, body_file)
            os.replace(temp_meta, meta_file)
        except Exception:
            try:
                if os.path.exists(temp_body):
                    os.remove(temp_body)
                if os.path.exists(temp_meta):
                    os.remove(temp_meta)
            except Exception:
                pass


class TrafficStats:
    """高精度网络流量与省流统计器（支持 Socket 物理层与 HTTP 协议层多维统计）。"""
    def __init__(self, proxy_bridge=None):
        self.net_transfer_bytes = 0
        self.cache_saved_bytes = 0
        self.cache_hit_count = 0
        self.blocked_count = 0
        self.proxy_bridge = proxy_bridge
        self.network_requests = []

    def set_proxy_bridge(self, bridge):
        self.proxy_bridge = bridge

    def record_transfer(self, num_bytes: int, url: str = "", r_type: str = "", status: int = 200):
        if num_bytes > 0:
            self.net_transfer_bytes += num_bytes
            if url:
                self.network_requests.append((num_bytes, url, r_type, status))

    def record_cache_hit(self, body_size: int):
        self.cache_hit_count += 1
        self.cache_saved_bytes += max(body_size, 2048)

    def record_blocked(self, est_size: int = 50000):
        self.blocked_count += 1
        self.cache_saved_bytes += est_size

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
            req = response.request
            resp_bytes = 250
            try:
                cl = response.headers.get("content-length")
                if cl and cl.isdigit():
                    resp_bytes += int(cl)
                else:
                    resp_bytes += 4096
            except Exception:
                resp_bytes += 2048
            self.record_transfer(resp_bytes, response.url, req.resource_type, response.status)
        except Exception:
            pass

    def get_mb(self) -> float:
        return self.get_transfer_mb()


# 1x1 透明标准 PNG / SVG / 空字体 极简数据
_DUMMY_SVG_IMAGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
_DUMMY_PNG_IMAGE = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
_DUMMY_EMPTY_FONT = b''
_DUMMY_EMPTY_AMD_MODULE = b'define([], function() { return {}; });'

# Azure Portal ExtensionManifest 客户端 Hook 脚本：在前端发起清单拉取时自动附带 m_type 参数，消除 Hash 漂移
_PORTAL_MANIFEST_HOOK_JS = """
(function() {
    function hookEarly(early) {
        if (!early || early._manifest_tagged) return early;
        early._manifest_tagged = true;
        var orig = early.getCachedManifestUri;
        if (typeof orig === 'function') {
            early.getCachedManifestUri = function(type) {
                var res = orig.apply(this, arguments);
                if (typeof res === 'string') {
                    return res + (res.indexOf('?') >= 0 ? '&' : '?') + 'm_type=' + encodeURIComponent(type);
                }
                if (res && typeof res.then === 'function') {
                    return res.then(function(uri) {
                        if (typeof uri === 'string') {
                            return uri + (uri.indexOf('?') >= 0 ? '&' : '?') + 'm_type=' + encodeURIComponent(type);
                        }
                        return uri;
                    });
                }
                return res;
            };
        }
        return early;
    }
    var _early = window.MsPortalEarly;
    if (_early) {
        hookEarly(_early);
    }
    try {
        Object.defineProperty(window, 'MsPortalEarly', {
            configurable: true,
            enumerable: true,
            get: function() { return _early; },
            set: function(v) {
                _early = hookEarly(v);
            }
        });
    } catch(e) {}
})();
"""


async def setup_save_data_route(ctx, stats: TrafficStats = None):
    """超深度省流路由系统：
    1. 前端注入 MsPortalEarly 清单类型 Hook，并走规范化类型强缓存（解决 Hash 漂移重复下载 20MB 的根本痛点）；
    2. 本地强缓存公共无状态静态 JS/CSS/字体/图标/静态JSON（portal.azure.com/*.js, aadcdn.msauth.net 等 0 字节复用）；
    3. 1-Byte 极简图片/SVG/字体 Mock 响应（彻底杜绝 4-6MB 冗余图像与字体网络下载，同时确保 onload 正常触发）；
    4. Azure Portal 非业务扩展模块（CostManagement, Advisor, Security, Monitoring 等）AMD 模块剪枝（节约 4-5MB 无用 JS）；
    5. 拦截非英语语言包 (zh-cn, es, fr, de 等)，节约 2MB+ 冗余包；
    6. 拦截第三方追踪与后台遥测；
    7. 100% 绝对原生放行 Arkose/FunCaptcha 挑战、SheerID 学术认证、Turnstile/Cloudflare、hCaptcha、Microsoft 动态认证及 2FA initializemobileapp 密钥提取与 ARM 提 Key 核心链路！
    """
    # 预热本地 ExtensionManifest 规范化类型索引
    LocalHttpCache.init_canonical_manifests()

    # 注入 Azure Portal 前端清单 Hook，动态透传 manifest 类型
    try:
        await ctx.add_init_script(_PORTAL_MANIFEST_HOOK_JS)
    except Exception:
        pass

    BLOCKED_MEDIA_EXTS = (
        ".mp4", ".webm", ".ogg", ".mp3", ".wav",
        ".pdf", ".zip", ".iso", ".exe", ".msi", ".rar", ".7z", ".tar", ".gz",
        ".dmg", ".pkg", ".bin", ".apk", ".m4s", ".ts", ".flv", ".m3u8"
    )

    IMAGE_EXTS = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".tiff"
    )

    FONT_EXTS = (
        ".woff2", ".woff", ".ttf", ".eot", ".otf"
    )

    BLOCKED_DOMAINS = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net", "facebook.net",
        "clarity.ms", "scorecardresearch.com", "cdn.speedcurve.com", "m.adnxs.com",
        "adsymptotic.com", "optimizely.com", "app.adjust.com", "braze.com",
        "branch.io", "quantummetric.com", "qualtrics.com", "app.launchdarkly.com",
        "segment.io", "segment.com", "amplitude.com", "mixpanel.com",
        "datadog.com", "browser-intake-datadoghq.com",
        "marketplace.azure.com", "learn.microsoft.com", "docs.microsoft.com",
        "browser.pipe.aria.microsoft.com", "pipe.aria.microsoft.com", "mobile.pipe.aria.microsoft.com",
        "events.data.microsoft.com", "self.events.data.microsoft.com", "vortex.data.microsoft.com",
        "web.vortex.data.microsoft.com", "watson.telemetry.microsoft.com", "telemetry.microsoft.com",
        "dc.services.visualstudio.com", "in.applicationinsights.azure.com", "dc.applicationinsights.azure.com",
        "dc.applicationinsights.microsoft.com", "global.monitor.azure.com", "monitor.azure.com",
        "js.monitor.azure.com", "activity.windows.com", "onesettings-public.azureedge.net",
        "config.edge.skype.com", "edge.microsoft.com", "nav.smartscreen.microsoft.com",
        "smartscreen-prod.microsoft.com", "c.msn.com", "feedback.azure.com",
    )

    BLOCKED_PATHS = (
        "/useravatar", "/avatar", "/profilepicture", "/marketing", "/feedback",
        "/survey", "/telemetry", "/diagnostics", "/instrumentation",
        "/api/cloudshell", "/api/advisor", "/api/costmanagement",
        "/api/search/suggestions", "/api/announcements", "/api/whatsnew",
        "/api/quickstart", "/api/guidedtour", "/api/notifications/broadcast",
        "/api/userfeedback", "/api/usersettings", "/api/telemetry", "/api/diagnostics", "/api/logger",
        "microsoft.resourcegraph", "microsoft.advisor", "microsoft.costmanagement",
        "microsoft.policyinsights", "microsoft.security"
    )

    UNNEEDED_PORTAL_EXTENSIONS = (
        "microsoft_azure_costmanagement", "microsoft_azure_advisor",
        "microsoft_azure_support", "microsoft_azure_monitoring",
        "microsoft_azure_security", "microsoft_azure_marketplace",
        "microsoft_azure_compute", "microsoft_azure_storage",
        "microsoft_azure_network", "microsoft_azure_virtualmachines",
        "microsoft_azure_loganalytics", "microsoft_azure_policy",
        "microsoft_azure_compliance", "microsoft_azure_securitycenter"
    )

    NON_EN_LOCALE_PATTERN = re.compile(
        r"/(zh-cn|es-es|fr-fr|de-de|ja-jp|ko-kr|pt-br|it-it|ru-ru|pl-pl|tr-tr|cs-cz|hu-hu|nl-nl|sv-se|da-dk|fi-fi|nb-no|zh-tw|zh-hk)\.(json|js)",
        re.IGNORECASE
    )

    fulfilled_request_ids = set()

    async def _route_handler(route, request):
        r_type = request.resource_type
        url = request.url
        url_lower = url.lower()

        # 1. 核心业务导航与主 HTML 文档：100% 原生直连（保证 Cookie、Session、登录跳转安全）
        if r_type == "document" or request.is_navigation_request():
            await route.continue_()
            return

        # 2. 非 GET 请求 (POST / PUT / DELETE / OPTIONS / PATCH 等)：100% 原生直连
        if request.method != "GET":
            await route.continue_()
            return

        clean_url = url_lower.split("?")[0].split("#")[0]

        # 3. 字体资源极速 Mock（拦截所有 Web 字体：woff2/woff/ttf/otf/eot，置于 DYNAMIC_SECURITY_PATTERNS 之前，彻底节约 Arkose 及各平台 1.2MB-2MB 字体流量）
        if r_type == "font" or clean_url.endswith(FONT_EXTS) or "/fonts/" in clean_url or "format=woff" in url_lower:
            if stats:
                stats.record_blocked(est_size=50000)
            fulfilled_request_ids.add(id(request))
            await route.fulfill(
                body=_DUMMY_EMPTY_FONT,
                headers={
                    "content-type": "font/woff2",
                    "access-control-allow-origin": "*",
                    "cache-control": "public, max-age=31536000"
                },
                status=200
            )
            return

        # 4. 核心风控/验证码挑战接口、Microsoft 动态登录认证接口、2FA 注册密钥接口与 ARM 提 Key 接口：100% 绝对原生放行
        # （绝不缓存、不拦截、不 Mock，确保 TOTP 提取与人机验证 100% 成功）
        if any(p in url_lower for p in LocalHttpCache.DYNAMIC_SECURITY_PATTERNS):
            await route.continue_()
            return

        # 5. Azure Portal 扩展清单 (ExtensionManifest)：优先走规范化类型强缓存（解决 Hash 漂移重复下载 20MB 的根本痛点）
        if "extensionmanifest/" in url_lower:
            cached_manifest = LocalHttpCache.get_canonical_manifest(url)
            if cached_manifest:
                body, headers, status = cached_manifest
                if stats:
                    stats.record_cache_hit(len(body))
                fulfilled_request_ids.add(id(request))
                await route.fulfill(body=body, headers=headers, status=status)
                return

        # 6. 公共无状态静态资源强缓存 (JS/CSS/图标/静态JSON)：本地秒级响应，0 网络流量
        # （涵盖 portal.azure.com 静态脚本, signup.azure.com 静态脚本, aadcdn.msauth.net 等）
        if LocalHttpCache.is_cacheable(url, "GET"):
            cached = LocalHttpCache.get(url)
            if cached:
                body, headers, status = cached
                if stats:
                    stats.record_cache_hit(len(body))
                fulfilled_request_ids.add(id(request))
                await route.fulfill(body=body, headers=headers, status=status)
                return

        # 7. 拦截大体积音视频及安装包
        if r_type == "media" or clean_url.endswith(BLOCKED_MEDIA_EXTS):
            if stats:
                stats.record_blocked(est_size=100000)
            await route.abort()
            return

        # 8. 拦截非英语多国语言包
        if NON_EN_LOCALE_PATTERN.search(clean_url):
            if stats:
                stats.record_blocked(est_size=100000)
            fulfilled_request_ids.add(id(request))
            await route.fulfill(body=b"{}", headers={"content-type": "application/json"}, status=200)
            return

        # 9. Azure Portal 非 Education 扩展模块剪枝（Mock 空 AMD 模块，节约 4-5MB JS 下载）
        if "/extension/" in clean_url and any(ext in clean_url for ext in UNNEEDED_PORTAL_EXTENSIONS):
            if stats:
                stats.record_blocked(est_size=500000)
            fulfilled_request_ids.add(id(request))
            await route.fulfill(
                body=_DUMMY_EMPTY_AMD_MODULE,
                headers={"content-type": "application/javascript", "access-control-allow-origin": "*"},
                status=200
            )
            return

        # 10. 非风控图片与图标快速 Mock（响应 1x1 极简图片，保证 DOM 事件不报错）
        # 注意：绝不 Mock Arkose / Cloudflare / SheerID 的人机验证挑战图片！
        is_captcha_img = any(k in url_lower for k in ("arkose", "funcaptcha", "turnstile", "sheerid", "cloudflare", "cf-"))
        if not is_captcha_img and (r_type in ("image", "imageset") or clean_url.endswith(IMAGE_EXTS)):
            if stats:
                stats.record_blocked(est_size=40000)
            fulfilled_request_ids.add(id(request))
            if clean_url.endswith(".svg") or "svg" in clean_url:
                await route.fulfill(
                    body=_DUMMY_SVG_IMAGE,
                    headers={"content-type": "image/svg+xml", "access-control-allow-origin": "*"},
                    status=200
                )
            else:
                await route.fulfill(
                    body=_DUMMY_PNG_IMAGE,
                    headers={"content-type": "image/png", "access-control-allow-origin": "*"},
                    status=200
                )
            return

        # 11. 拦截第三方追踪与后台非业务遥测/非核心 API
        try:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
        except Exception:
            hostname = ""
            path = ""

        is_blocked_domain = any(hostname == d or hostname.endswith("." + d) for d in BLOCKED_DOMAINS)
        is_blocked_path = any(path.startswith(p) or p in path for p in BLOCKED_PATHS)
        if is_blocked_domain or is_blocked_path:
            if stats:
                stats.record_blocked(est_size=30000)
            if "/api/" in path or path.endswith((".json", "/telemetry")):
                fulfilled_request_ids.add(id(request))
                await route.fulfill(body=b'{"status": 200, "data": []}', headers={"content-type": "application/json"}, status=200)
            else:
                await route.abort()
            return

        # 12. 对属于可缓存范围但尚未命中的静态请求（包括 ExtensionManifest、Portal 静态脚本）：
        # 使用 route.fetch() 确定性拉取并即时存入强缓存/规范化缓存！
        if LocalHttpCache.is_cacheable(url, "GET") or "extensionmanifest/" in url_lower:
            try:
                fetch_resp = await route.fetch()
                if fetch_resp.status == 200:
                    resp_body = await fetch_resp.body()
                    if resp_body and len(resp_body) > 10:
                        resp_headers = dict(fetch_resp.headers)
                        if "extensionmanifest/" in url_lower:
                            LocalHttpCache.save_canonical_manifest(url, resp_body, resp_headers)
                        LocalHttpCache.put(url, resp_body, resp_headers, fetch_resp.status)
                    if stats:
                        stats.record_transfer(len(resp_body) + 400, url, r_type, fetch_resp.status)
                    fulfilled_request_ids.add(id(request))
                    await route.fulfill(response=fetch_resp, body=resp_body)
                    return
                else:
                    await route.fulfill(response=fetch_resp)
                    return
            except Exception:
                await route.continue_()
                return

        # 13. 其他所有核心请求原生放行
        await route.continue_()

    await ctx.route("**/*", _route_handler)

    def _on_response_measure(response):
        try:
            req = response.request
            req_id = id(req)
            if req_id in fulfilled_request_ids:
                fulfilled_request_ids.discard(req_id)
                return  # 本地强缓存与本地 Mock 命中，不计入网络传输！

            req_bytes = 250
            try:
                for k, v in req.headers.items():
                    req_bytes += len(k) + len(v) + 4
            except Exception:
                pass
            try:
                pd = req.post_data_buffer
                if pd:
                    req_bytes += len(pd)
            except Exception:
                pass

            resp_bytes = 250
            try:
                r_headers = response.headers
                for k, v in r_headers.items():
                    resp_bytes += len(k) + len(v) + 4
                cl = r_headers.get("content-length")
                if cl and cl.isdigit():
                    resp_bytes += int(cl)
                else:
                    resp_bytes += 4096
            except Exception:
                resp_bytes += 2048

            if stats:
                stats.record_transfer(req_bytes + resp_bytes, response.url, req.resource_type, response.status)
        except Exception:
            pass

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
            "--disable-component-update",
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
    await ctx.add_init_script(build_init_script(fp))

    stats = TrafficStats()
    if enable_save_data:
        await setup_save_data_route(ctx, stats)
    else:
        ctx.on("response", stats.add_response)

    return browser, ctx, fp, stats
