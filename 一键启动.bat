@echo off
chcp 65001 >nul
title Azure Key 提取工具
cd /d "%~dp0"

echo ================================================
echo   Azure Education Key 提取工具
echo   正在初始化...
echo ================================================
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0python\python.exe" (
    set "PYEXE=%~dp0python\python.exe"
) else (
    set "PYEXE=python"
)

echo [Info] 使用 Python: %PYEXE%
echo [Info] 访问地址: http://localhost:5010
echo [Info] 按 Ctrl+C 停止服务
echo.

"%PYEXE%" run_gui.py

if errorlevel 1 (
    echo.
    echo [提示] 服务出现异常退出，错误代码: %ERRORLEVEL%
    pause
)