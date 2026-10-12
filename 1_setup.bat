@echo off
cd /d "%~dp0"
echo === Company Operator setup === > setup.log
where py >nul 2>nul && (set PY=py -3) || (set PY=python)

echo [1/5] Creating the Python environment...
if not exist .venv (%PY% -m venv .venv >> setup.log 2>&1)
if not exist .venv\Scripts\activate.bat goto fail_venv
call .venv\Scripts\activate.bat
python --version >> setup.log 2>&1

echo [2/5] Installing libraries (a few minutes)...
python -m pip install --upgrade pip >> setup.log 2>&1
python -m pip install -r requirements.txt >> setup.log 2>&1
if errorlevel 1 goto fail_pip

echo [3/5] Installing the Chromium browser...
python -m playwright install chromium >> setup.log 2>&1
if errorlevel 1 goto fail_browser

echo [4/5] Checking the Gemini key...
python scripts\gemini_check.py >> setup.log 2>&1
if errorlevel 1 goto fail_key

echo [5/5] Done.
echo SETUP_DONE >> setup.log
echo Setup finished successfully. Next: double-click 2_start_console.bat
pause
exit /b 0

:fail_venv
echo SETUP_FAILED: could not create .venv (is Python 3.11+ installed and on PATH?) >> setup.log
echo SETUP FAILED: could not create the Python environment. Install Python 3.11+ with "Add to PATH" ticked. See setup.log
pause
exit /b 1
:fail_pip
echo SETUP_FAILED: pip install -r requirements.txt failed >> setup.log
echo SETUP FAILED: installing libraries failed (check the internet connection). See setup.log
pause
exit /b 1
:fail_browser
echo SETUP_FAILED: playwright install chromium failed >> setup.log
echo SETUP FAILED: installing the Chromium browser failed. See setup.log
pause
exit /b 1
:fail_key
echo SETUP_FAILED: Gemini check failed >> setup.log
echo Libraries are installed, but the Gemini check failed: check GEMINI_API_KEY and GEMINI_MODEL in .env.
echo Offline tests still work without a key. See setup.log
pause
exit /b 1
