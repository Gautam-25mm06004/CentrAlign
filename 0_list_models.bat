@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python scripts\list_models.py > models.log 2>&1
type models.log
pause
