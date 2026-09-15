@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo [停止] 优雅关停（标志文件 探 信号 探 强杀，T1.12）
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\stop_all.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo [停止] 有进程被**强制终止**（退出码 %EXITCODE%）—— 它可能丢了当前单元，看 data\logs\*.log
)
echo.
pause
exit /b %EXITCODE%
