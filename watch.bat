@echo off
chcp 65001 >nul
call venv\Scripts\activate.bat
echo بدء المراقبة... (اضغط Ctrl+C للإيقاف)
python main.py watch
pause
