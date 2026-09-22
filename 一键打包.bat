@echo off
chcp 65001 >nul
title Azure Key Extractor 打包工具
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

echo ========================================================
echo        Azure Key 提取工具 - 一键打包绿色免安装便携版
echo ========================================================
echo.
echo 请选择打包模式：
echo   [1] 轻量便携版 (~55MB)
echo       体积小，适合微信/邮件发送；使用接收方系统的 Chrome/Edge 浏览器。
echo.
echo   [2] 全内置离线版 (~160MB) [推荐]
echo       内置独立 Chromium 浏览器内核，接收方 100% 免配置、防通行密钥弹窗。
echo.
echo   [3] 同时打包以上两个版本
echo.
set /p choice="请输入数字选项 (默认 2): "

if "%choice%"=="" set choice=2
if "%choice%"=="1" goto LITE
if "%choice%"=="2" goto FULL
if "%choice%"=="3" goto BOTH

echo 无效选项，默认选择 [2] 全内置离线版
goto FULL

:LITE
echo.
"%PYEXE%" build_package.py --mode lite
goto END

:FULL
echo.
"%PYEXE%" build_package.py --mode full
goto END

:BOTH
echo.
"%PYEXE%" build_package.py --mode both
goto END

:END
if errorlevel 1 (
    echo.
    echo [错误] 打包过程中出现异常，请检查上方报错信息。
    pause
    exit /b 1
)

echo.
echo ========================================================
echo 打包完成！即将为您打开输出目录: dist
echo ========================================================
explorer "%~dp0dist"
pause