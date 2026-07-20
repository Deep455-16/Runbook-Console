@echo off
echo Starting Runbook Chatbot Server...
python -m uvicorn server:app --reload --port 8000
pause
