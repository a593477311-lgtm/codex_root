@echo off
rem 手动启动 Codex<->Kimi 桥接（后台无窗口）
set PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe
if not exist "%PYW%" set PYW=pythonw.exe
start "" "%PYW%" "%USERPROFILE%\.codex\tools\kimi_bridge.py"
timeout /t 4 /nobreak >nul
netstat -ano | findstr ":15731.*LISTENING" >nul && echo [OK] bridge listening on 15731 || echo [FAIL] not listening, check %USERPROFILE%\.codex\tools\kimi_bridge.log
