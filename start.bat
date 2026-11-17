@echo off
cd /d "%~dp0"
title Ledgerline server - keep this window open
echo Starting Ledgerline... keep this window open while people use the app.
".venv\Scripts\python.exe" -u serve.py
pause
