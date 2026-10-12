@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m evaluation.run_eval --suite school-live --engine gemini --max-calls 90 > data\school_live.log 2>&1
echo LIVE_EVAL_DONE >> data\school_live.log
type data\school_live.log
pause
