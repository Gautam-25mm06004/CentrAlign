@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m evaluation.run_eval --suite live-smoke --engine gemini --max-calls 170 > data\live_eval.log 2>&1
echo LIVE_EVAL_DONE >> data\live_eval.log
type data\live_eval.log
pause
