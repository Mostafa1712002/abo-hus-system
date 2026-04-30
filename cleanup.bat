@echo off
chcp 65001 >nul
cd /d "C:\Users\aldaa\data\projects\youtube-auto-uploader"
set PYTHONIOENCODING=utf-8
call venv\Scripts\activate.bat
echo ============ %date% %time% ============ >> logs\cleanup.log
python -m src.cleanup >> logs\cleanup.log 2>&1
