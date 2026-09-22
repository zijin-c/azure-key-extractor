import os
import sys
import pathlib
import glob
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = pathlib.Path(__file__).resolve().parent

# ── 浏览器 ───────────────────────────────────────────────────
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
SLOW_MO  = int(os.getenv("SLOW_MO", "20"))
TIMEOUT  = int(os.getenv("TIMEOUT", "60000"))
ENABLE_SAVE_DATA = os.getenv("ENABLE_SAVE_DATA", "true").lower() == "true"

# 代理（用于 Azure 认证浏览器）
# 格式: socks5://user:pass@host:port  或  http://host:port
PROXY = os.getenv("PROXY", "").strip() or None

# VLESS 前置代理（国内无法直连 SOCKS5 时使用）
VLESS_PROXY = os.getenv("VLESS_PROXY", "").strip() or None

def parse_proxy_list(proxy_input: str | None) -> list[str]:
    """解析代理池字符串为代理列表（支持换行、逗号、分号分隔）。"""
    if not proxy_input:
        return []
    import re
    items = re.split(r"[\r\n,;\s]+", proxy_input.strip())
    result = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        if not item.startswith(("http://", "https://", "socks5://", "socks4://")):
            item = "http://" + item
        result.append(item)
    return result

# ── Chromium 路径自动检测 ─────────────────────────────────────
def _find_dedicated_chromium() -> str | None:
    """专门检索 Playwright 的 Dedicated Chromium 浏览器实例（非系统自带的 Edge/Chrome）。"""
    home = pathlib.Path.home()
    search_bases = []
    pw_env = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if pw_env:
        search_bases.append(pathlib.Path(pw_env))
    search_bases.append(BASE_DIR / "ms-playwright")
    
    if sys.platform == "win32":
        search_bases.append(home / "AppData" / "Local" / "ms-playwright")
        for base in search_bases:
            if not base.exists():
                continue
            pattern = str(base / "chromium-*" / "chrome-win*" / "chrome.exe")
            candidates = sorted(glob.glob(pattern), reverse=True)
            if candidates:
                return candidates[0]
    else:
        search_bases.extend([
            home / ".cache" / "ms-playwright",
            home / "Library" / "Caches" / "ms-playwright"
        ])
        for base in search_bases:
            if not base.exists():
                continue
            for pattern in [
                str(base / "chromium-*" / "chrome-linux" / "chrome"),
                str(base / "chromium-*" / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"),
            ]:
                candidates = sorted(glob.glob(pattern), reverse=True)
                if candidates:
                    return candidates[0]
    return None


def _find_chromium() -> str | None:
    dedicated = _find_dedicated_chromium()
    if dedicated:
        return dedicated

    home = pathlib.Path.home()
    if sys.platform == "win32":
        alt_paths = [
            # Google Chrome（只匹配 Google Chrome，绝不上退回系统的 Edge）
            home / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe",
            pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            pathlib.Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
        for p in alt_paths:
            if p.exists():
                return str(p)
    else:
        for p in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]:
            if os.path.exists(p):
                return p
    return None


def get_chromium_path() -> str | None:
    env_path = os.getenv("CHROMIUM_PATH", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path
    return _find_chromium()


def ensure_chromium_installed() -> str | None:
    """自动检测 Playwright Chromium 浏览器，若未检测到则在后台秒级全自动补全下载。"""
    found = get_chromium_path()
    if found:
        return found

    print("[提示] 未检测到 Playwright Chromium 独立浏览器，正在启动全自动触发下载补全...")
    import subprocess
    target_dir = BASE_DIR / "ms-playwright"
    target_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(target_dir)

    try:
        if sys.platform != "win32":
            subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=False)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        print("[OK] Playwright Chromium 浏览器全自动下载并补全成功！")
    except Exception as e:
        print(f"[警告] 自动下载 Chromium 过程中出现提示: {e}")

    return _find_chromium()


CHROMIUM_PATH = ensure_chromium_installed()


# ── Azure 相关 URL ────────────────────────────────────────────
# 第一步：直接打开 Azure for Students 学生认证入口
AZURE_SIGNUP_URL = "https://signup.azure.com/studentverification?offerType=1"
# Azure 登录后端直连入口（直接 302 毫秒级重定向至微软 OAuth 登录页）
AZURE_SIGNUP_LOGIN_URL = (
    "https://signup.azure.com/api/user/login"
    "?returnUrl=https%3A%2F%2Fsignup.azure.com%2Fstudentverification%3FofferType%3D1"
)

# 第二步：Azure Education Software 页面（提取 key）
# 注意：portal 是 SPA，# 后面是前端路由，需先加载 portal 再导航
AZURE_EDU_SOFTWARE_URL = (
    "https://portal.azure.com/#view/Microsoft_Azure_Education"
    "/EducationMenuBlade/~/software"
)

# ── 要提取 key 的产品列表（精确名称匹配）────────────────────
PRODUCTS_TO_EXTRACT = [
    "Visio Professional 2021",
    "Project Professional 2021 - DVD",
    "Windows Server 2022 Standard (updated July 2023)",
    "Windows Server 2025 Standard",
    "Windows Server 2019 Standard (updated Mar 2023)",
    "Windows 11 Education, version 25H2",
]

# ── 并发 ─────────────────────────────────────────────────────
CONCURRENCY = int(os.getenv("CONCURRENCY", "1"))

# ── 超时 ─────────────────────────────────────────────────────
PAGE_LOAD_TIMEOUT  = int(os.getenv("PAGE_LOAD_TIMEOUT", "90000"))
ELEMENT_TIMEOUT    = int(os.getenv("ELEMENT_TIMEOUT", "30000"))
PORTAL_WAIT        = int(os.getenv("PORTAL_WAIT", "120"))   # 等待 portal 加载最大秒数

# ── 批次运行与自动重启 ─────────────────────────────────────────
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "0"))  # 批次大小（默认 0 表示不分批一次性连续跑完；若 >0 则按批次划分）
AUTO_RESTART_ON_BATCH = os.getenv("AUTO_RESTART_ON_BATCH", "false").lower() == "true"  # 批次完成自动强制重启（默认已关闭）
AUTO_RESTART_ON_COMPLETE = os.getenv("AUTO_RESTART_ON_COMPLETE", "false").lower() == "true"  # 全部完成自动强制重启（默认关闭，避免产生多余僵尸后台 Python 进程）


