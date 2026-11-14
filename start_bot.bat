@echo off
cd /d "%~dp0"

set "PY=%USERPROFILE%\Python314\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -c "import aiogram, PIL, dotenv, tzdata" 2>nul
if errorlevel 1 "%PY%" -m pip install --disable-pip-version-check -r requirements.txt

"%PY%" bot.py
pause
