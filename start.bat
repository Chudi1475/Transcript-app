@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python app.py >> server.log 2>&1
