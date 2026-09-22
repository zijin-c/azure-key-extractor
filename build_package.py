"""
Azure Key Extractor - 绿色免安装独立便携版打包脚本
生成结果: dist/AzureKeyExtractor_v1.0_Portable.zip (约 30-40MB)
自带独立 Embedded Python 运行环境 + 预装全部第三方依赖包 + 自动匹配系统 Edge / Chrome 浏览器
100% 解压即用，绝无环境与路径报错！
"""
import os
import sys
import shutil
import pathlib
import subprocess
import zipfile
import urllib.request

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
SCRATCH_DIR = PROJECT_ROOT / "scratch"
DIST_ROOT = PROJECT_ROOT / "dist"
PACKAGE_NAME = "AzureKeyExtractor_Portable"
PACKAGE_DIR = DIST_ROOT / PACKAGE_NAME
ZIP_PATH = DIST_ROOT / "AzureKeyExtractor_v1.0_Portable.zip"

PYTHON_EMBED_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def log(msg: str):
    print("==================================================")
    print(f"  {msg}")
    print("==================================================")


def clean_previous_build():
    log("1/5 清理旧构建目录...")
    if PACKAGE_DIR.exists():
        print(f"正在删除旧目录: {PACKAGE_DIR}")
        shutil.rmtree(PACKAGE_DIR, ignore_errors=True)
    if ZIP_PATH.exists():
        print(f"正在删除旧压缩包: {ZIP_PATH}")
        os.remove(ZIP_PATH)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)


def setup_embedded_python():
    log("2/5 准备 100% 独立脱机 Python 运行环境 (Embedded Python)...")
    py_zip_file = SCRATCH_DIR / "python-3.11.9-embed-amd64.zip"
    if not py_zip_file.exists() or py_zip_file.stat().st_size < 10000000:
        print("下载 Python 3.11 独立便携运行时...")
        urllib.request.urlretrieve(PYTHON_EMBED_URL, py_zip_file)
    
    target_py_dir = PACKAGE_DIR / "python"
    target_py_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"解压 Python 运行时至: {target_py_dir}")
    with zipfile.ZipFile(py_zip_file, "r") as zf:
        zf.extractall(target_py_dir)

    # 修改 python311._pth 以启用 site-packages
    pth_file = target_py_dir / "python311._pth"
    pth_content = "python311.zip\n.\n..\nLib\\site-packages\nimport site\n"
    with open(pth_file, "w", encoding="utf-8") as f:
        f.write(pth_content)

    # 补全 C Runtime (MSVC) 关键 DLL 库，解决在干净系统上 _greenlet 报 DLL load failed 的问题
    sys32 = pathlib.Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32"
    msvc_dlls = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_codecvt_ids.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll"]
    for dll in msvc_dlls:
        src_dll = sys32 / dll
        if src_dll.exists():
            try:
                shutil.copy2(src_dll, target_py_dir / dll)
                print(f"补全 C++ Runtime DLL: {dll}")
            except Exception as e:
                print(f"复制 {dll} 警告: {e}")

    return target_py_dir / "python.exe"


def install_dependencies(embed_python: pathlib.Path):
    log("3/5 预装全部第三方依赖包 (Flask, Playwright, PyOTP 等)...")
    get_pip_file = SCRATCH_DIR / "get-pip.py"
    if not get_pip_file.exists():
        print("下载 get-pip.py 引导工具...")
        urllib.request.urlretrieve(GET_PIP_URL, get_pip_file)

    print("安装 pip 工具...")
    subprocess.run([str(embed_python), str(get_pip_file)], check=True)

    print("预装 requirements.txt 中的第三方库...")
    req_file = PROJECT_ROOT / "requirements.txt"
    subprocess.run([str(embed_python), "-m", "pip", "install", "-r", str(req_file)], check=True)

    # 移除 setuptools 产生的 distutils-precedence.pth，防止 embedded python 初始化 ModuleNotFoundError: No module named '_distutils_hack'
    site_packages = embed_python.parent / "Lib" / "site-packages"
    pth_file = site_packages / "distutils-precedence.pth"
    if pth_file.exists():
        print("清理 distutils-precedence.pth 消除兼容性隐患...")
        try:
            pth_file.unlink()
        except Exception:
            pass

    # 验证引入
    print("校验嵌入式环境安装可用性...")
    test_cmd = [
        str(embed_python), "-c",
        "import flask, playwright, pyotp, openpyxl, requests, dotenv, socks; print('[OK] 依赖库校验全部正常')"
    ]
    subprocess.run(test_cmd, check=True)


def copy_source_files(include_browser: bool = False):
    log("4/5 复制项目源码及配置文件...")
    
    directories_to_copy = ["gui", "modules", "utils"]
    for d in directories_to_copy:
        src = PROJECT_ROOT / d
        dst = PACKAGE_DIR / d
        if src.exists():
            print(f"复制目录: {d} -> {dst}")
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 打包独立 Playwright Chromium 浏览器（全内置离线版）
    if include_browser:
        src_pw = PROJECT_ROOT / "ms-playwright"
        dst_pw = PACKAGE_DIR / "ms-playwright"
        if src_pw.exists():
            chrom_dirs = list(src_pw.glob("chromium-*"))
            if chrom_dirs:
                dst_pw.mkdir(parents=True, exist_ok=True)
                for c_dir in chrom_dirs:
                    target_chrom = dst_pw / c_dir.name
                    print(f"打包独立 Chromium 浏览器内核: {c_dir.name} -> {target_chrom}")
                    shutil.copytree(c_dir, target_chrom, dirs_exist_ok=True)
            else:
                print("警告: 本地未找到 ms-playwright/chromium-* 目录，跳过浏览器内置")
        else:
            print("警告: 未找到 ms-playwright 目录，跳过浏览器内置")

    files_to_copy = ["config.py", "run_gui.py", "requirements.txt", "DEPLOY.md"]
    for f in files_to_copy:
        src = PROJECT_ROOT / f
        dst = PACKAGE_DIR / f
        if src.exists():
            print(f"复制文件: {f}")
            shutil.copy2(src, dst)

    # 备份 get-pip.py 方便本地自动修复
    get_pip_src = SCRATCH_DIR / "get-pip.py"
    if get_pip_src.exists():
        shutil.copy2(get_pip_src, PACKAGE_DIR / "python" / "get-pip.py")

    env_src = PROJECT_ROOT / ".env"
    env_example = PROJECT_ROOT / ".env.example"
    env_dst = PACKAGE_DIR / ".env"
    env_example_dst = PACKAGE_DIR / ".env.example"
    
    if env_example.exists():
        shutil.copy2(env_example, env_example_dst)
    if env_src.exists():
        shutil.copy2(env_src, env_dst)
    elif env_example.exists():
        shutil.copy2(env_example, env_dst)

    (PACKAGE_DIR / "data").mkdir(exist_ok=True)
    cache_src = PROJECT_ROOT / "data" / "python_http_cache"
    cache_dst = PACKAGE_DIR / "data" / "python_http_cache"
    if cache_src.exists():
        print("打包静态 HTTP 强缓存与规范化 ExtensionManifest...")
        shutil.copytree(cache_src, cache_dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.tmp"))
    (PACKAGE_DIR / "results").mkdir(exist_ok=True)
    (PACKAGE_DIR / "screenshots").mkdir(exist_ok=True)

    launcher_content = """@echo off\r
chcp 65001 >nul\r
title Azure Key 提取工具 (绿色免安装便携版)\r
cd /d "%~dp0"\r
\r
set "PYTHONIOENCODING=utf-8"\r
set "PYEXE=%~dp0python\\python.exe"\r
\r
if not exist "%PYEXE%" (\r
    echo [错误提示] 未能在当前目录找到内置 Python 运行环境！\r
    echo [可能原因] 1. 您直接在 zip 压缩包内部双击了运行。\r
    echo            2. 压缩包未解压完整。\r
    echo [解决方案] 请务必先将 ZIP 压缩包【解压到独立文件夹】，\r
    echo            然后再双击进入文件夹运行【一键启动.bat】！\r
    echo.\r
    pause\r
    exit /b 1\r
)\r
\r
echo ==================================================\r
echo   Azure Key 提取工具 (绿色免安装便携版)\r
echo ==================================================\r
echo [Start] 正在启动后台服务...\r
echo [Info]  默认调取 Playwright / 系统浏览器与随机指纹\r
echo [Info]  服务启动后，浏览器将自动打开: http://localhost:5010\r
echo [Info]  按 Ctrl+C 或关闭本窗口可停止服务\r
echo.\r
\r
"%PYEXE%" run_gui.py\r
\r
if errorlevel 1 (\r
    echo.\r
    echo [提示] 服务出现异常退出，错误代码: %ERRORLEVEL%\r
)\r
\r
echo.\r
echo [提示] 服务已停止，按任意键关闭窗口...\r
pause\r
"""

    launcher_path = PACKAGE_DIR / "一键启动.bat"
    print(f"生成 UTF-8 启动脚本: {launcher_path.name}")
    with open(launcher_path, "w", encoding="utf-8", newline="") as f:
        f.write(launcher_content)


def compress_to_zip(target_zip: pathlib.Path):
    log("5/5 打包压缩为绿色脱机 ZIP 文件...")
    if target_zip.exists():
        try:
            os.remove(target_zip)
        except Exception:
            pass
    print(f"压缩中: {target_zip.name} (请稍候...)")
    
    ignore_names = {"__pycache__", ".git", ".venv", ".idea", ".vscode"}
    ignore_exts = {".pyc", ".pyo", ".pdb"}

    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(PACKAGE_DIR):
            dirs[:] = [d for d in dirs if d not in ignore_names]
            for file in files:
                ext = pathlib.Path(file).suffix.lower()
                if ext in ignore_exts:
                    continue
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, DIST_ROOT)
                zipf.write(file_path, arcname)

    size_mb = target_zip.stat().st_size / (1024 * 1024)
    print(f"\n[OK] 便携包打包完成！")
    print(f"便携目录: {PACKAGE_DIR}")
    print(f"Zip压缩包: {target_zip} ({size_mb:.2f} MB)")
    print(f"直接把 【{target_zip.name}】 发给对方，对方解压后双击【一键启动.bat】直接使用！")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Azure Key Extractor 打包工具")
    parser.add_argument("--mode", choices=["lite", "full", "both"], default="lite",
                        help="lite: 轻量版(~55MB，使用系统Chrome/Edge或在线下载); full: 全内置离线版(~160MB，包含Chromium内核); both: 同时生成两版")
    parser.add_argument("--clean", action="store_true", help="是否彻底清空旧环境重新下载组装")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        embed_python = PACKAGE_DIR / "python" / "python.exe"
        if args.clean or not embed_python.exists():
            clean_previous_build()
            embed_python = setup_embedded_python()
            install_dependencies(embed_python)
        else:
            print("[INFO] 复用已有的独立 Python 运行环境，快速打包...")

        modes = ["lite", "full"] if args.mode == "both" else [args.mode]

        for m in modes:
            is_full = (m == "full")
            if is_full:
                zip_target = DIST_ROOT / "AzureKeyExtractor_v1.0_Full_Portable.zip"
                print("\n" + "=" * 50)
                print(">> 开始打包: 全内置离线版 (包含 Chromium 浏览器内核)")
                print("=" * 50)
            else:
                zip_target = DIST_ROOT / "AzureKeyExtractor_v1.0_Portable.zip"
                print("\n" + "=" * 50)
                print(">> 开始打包: 轻量便携版 (约 50MB，使用系统浏览器)")
                print("=" * 50)

            # 清理旧的 ms-playwright 目录以防模式冲突
            old_pw = PACKAGE_DIR / "ms-playwright"
            if old_pw.exists():
                shutil.rmtree(old_pw, ignore_errors=True)

            copy_source_files(include_browser=is_full)
            compress_to_zip(zip_target)

    except Exception as e:
        print(f"\n[ERROR] 打包失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
