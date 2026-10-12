@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m evaluation.run_eval --suite live-after-review --engine gemini --max-calls 60 > data\live_after_review.log 2>&1
echo LIVE_EVAL_DONE >> data\live_after_review.log
type data\live_after_review.log
pause
