@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo [启动] doctor 门禁 探 拉起服务 探 打开浏览器（T1.12）
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\start_all.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo [启动] 未成功（退出码 %EXITCODE%）—— 看上面的提示与 data\logs\*.log
) else (
    echo [启动] 完成。WebUI: http://127.0.0.1:8787/
)
echo.
pause
exit /b %EXITCODE%
