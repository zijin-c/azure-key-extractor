"""
Azure Key 提取工具 - 启动入口
运行: python run_gui.py
然后浏览器访问: http://localhost:5010
"""
import os
import sys
import traceback

# Linux 内存优化：限制 glibc 内存分配区数量，避免多线程导致虚拟/物理内存虚高和碎片不释放
if sys.platform != "win32":
    os.environ.setdefault("MALLOC_ARENA_MAX", "2")
    os.environ.setdefault("MALLOC_TRIM_THRESHOLD_", "131072")

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_xvfb_proc = None


def _setup_xvfb():
    """Linux 环境下虚拟显示管理。仅在非无头模式（HEADLESS=false）下才需要启动。"""
    global _xvfb_proc
    if sys.platform == "win32":
        return
    if os.environ.get("DISPLAY"):
        return  # 已有显示环境

    # 检测是否为无头模式：生产 VPS 推荐无头模式，Playwright 原生支持无头运行，完全不需要 Xvfb
    headless_env = os.environ.get("HEADLESS", "").strip().lower()
    if headless_env in ("true", "1", "yes"):
        return

    # 若未设置 HEADLESS 环境变量，检测 config.py 中的配置
    try:
        from config import HEADLESS
        if HEADLESS:
            return
    except Exception:
        pass

    try:
        import subprocess
        # 检查 Xvfb 是否安装
        result = subprocess.run(["which", "Xvfb"], capture_output=True)
        if result.returncode != 0:
            print("⚠️  未安装 Xvfb，自动切换为无头模式 (HEADLESS=true)")
            os.environ["HEADLESS"] = "true"
            return

        # 启动轻量级 Xvfb (使用更合理的 1280x720x16 分辨率，大幅降低虚拟显存缓冲区开销)
        display = ":99"
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x720x16", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        os.environ["DISPLAY"] = display
        print(f"✅ Xvfb 虚拟显示已启动 (DISPLAY={display}, 优化分辨率 1280x720x16)")

        import atexit
        def _cleanup_xvfb():
            global _xvfb_proc
            if _xvfb_proc:
                try:
                    _xvfb_proc.terminate()
                    _xvfb_proc.wait(timeout=2)
                except Exception:
                    try:
                        _xvfb_proc.kill()
                    except Exception:
                        pass
                _xvfb_proc = None

        atexit.register(_cleanup_xvfb)
    except Exception as e:
        print(f"⚠️  Xvfb 启动失败: {e}，将使用无头模式")
        os.environ["HEADLESS"] = "true"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 检测并优先配置项目根目录下的 ms-playwright 浏览器路径
    project_root = os.path.dirname(os.path.abspath(__file__))
    local_ms_pw = os.path.join(project_root, "ms-playwright")
    os.makedirs(local_ms_pw, exist_ok=True)
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_ms_pw

    # 自动检测专用的 Playwright Chromium，若缺失则自动全自动下载配置
    try:
        from config import _find_dedicated_chromium
        if not _find_dedicated_chromium():
            print("\n[首次运行/自动配置] 正在为您自动下载专用的 Chromium 独立浏览器（彻底规避系统 Edge/Passkey 弹窗）...")

            import subprocess
            py_exe = sys.executable
            subprocess.run([py_exe, "-m", "playwright", "install", "chromium"], check=False)
            print("[OK] 专用 Chromium 浏览器自动配置完成！\n")
    except Exception as b_err:
        pass

    # Linux VPS 自动启动虚拟显示
    _setup_xvfb()



    try:
        import webbrowser
        import threading
        import time
        from gui.app import app
    except ImportError as e:
        print(f"\n[提示] 检测到关键依赖组件缺失 ({e})，正在尝试为您自动安装修复...")
        try:
            import subprocess
            py_exe = sys.executable
            req_file = os.path.join(project_root, "requirements.txt")
            if os.path.exists(req_file):
                subprocess.run([py_exe, "-m", "pip", "install", "-r", req_file], check=True)
                from gui.app import app
                print("✅ 依赖组件自动修复完成！继续启动服务...")
            else:
                raise FileNotFoundError("未找到 requirements.txt")
        except Exception as repair_err:
            print(f"\n*** 缺少依赖包且自动修复失败: {repair_err} ***")
            print(f"提示: 请务必先将【AzureKeyExtractor_v1.0_Portable.zip】完全解压到独立文件夹后再运行【一键启动.bat】。\n")
            if sys.platform == "win32" and sys.stdin and sys.stdin.isatty():
                try:
                    input("按回车键退出...")
                except Exception:
                    pass
            sys.exit(1)
    except Exception as e:
        print(f"\n*** 启动失败 ***")
        traceback.print_exc()
        if sys.platform == "win32" and sys.stdin and sys.stdin.isatty():
            try:
                input("按回车键退出...")
            except Exception:
                pass
        sys.exit(1)

    def _open_browser():
        time.sleep(1.5)
        webbrowser.open("http://localhost:5010")

    print("=" * 55)
    print("  Azure Education Key 提取工具")
    print("  访问地址: http://localhost:5010")
    print("=" * 55)
    print("  按 Ctrl+C 停止服务")
    print()

    # Windows 下自动打开浏览器（仅在初次启动时打开，批次重启恢复时不重复弹窗）
    if sys.platform == "win32":
        try:
            from modules.batch_manager import BatchManager
            is_restarting = BatchManager.is_running()
        except Exception:
            is_restarting = False
        if not is_restarting:
            threading.Thread(target=_open_browser, daemon=True).start()

    try:
        app.run(host="0.0.0.0", port=5010, debug=False, threaded=True)
    except OSError as e:
        if "10048" in str(e) or "Address already in use" in str(e):
            print(f"\n*** 端口 5010 已被占用，请关闭其他程序后重试 ***")
        else:
            print(f"\n*** 服务启动失败: {e} ***")
        if sys.platform == "win32" and sys.stdin and sys.stdin.isatty():
            try:
                input("按回车键退出...")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
