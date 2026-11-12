@echo off
echo [*] Stopping F1 Pit Wall service on port 5000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr 0.0.0.0:5000') do (
    echo [*] Terminating Process ID: %%a
    taskkill /f /pid %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr 127.0.0.1:5000') do (
    echo [*] Terminating Process ID: %%a
    taskkill /f /pid %%a >nul 2>&1
)
echo [OK] F1 Pit Wall server stopped.
pause
