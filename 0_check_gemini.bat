@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python scripts\gemini_check.py > gemini_check.log 2>&1
type gemini_check.log
pause
