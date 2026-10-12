@echo off
cd /d "%~dp0"
echo Stopping services on ports 8100-8103 (this project only)...
for %%P in (8100 8101 8102 8103) do for /f "tokens=5" %%A in ('netstat -ano ^| findstr /R /C:"127.0.0.1:%%P .*LISTENING"') do taskkill /PID %%A /F >nul 2>&1
timeout /t 2 >nul
call .venv\Scripts\activate.bat
python start.py
pause
