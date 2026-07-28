@echo off
cd /d "%~dp0"
rem pin the venv python directly — `call activate` + bare `python` can silently
rem resolve to the Windows Store python instead (it did)
.venv\Scripts\python.exe app.py >> server.log 2>&1
