@echo off
chcp 65001 >nul
cd /d "%~dp0"
python app.py --port 8765
pause
