@echo off
rem 停止运行在 15731 端口的 Codex 网关服务
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":15731.*LISTENING"') do (
    echo Terminating PID: %%a
    taskkill /F /PID %%a
)
echo [OK] Bridge stopped.
