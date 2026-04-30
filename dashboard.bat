@echo off
chcp 65001 >nul
cd /d "%~dp0"
call venv\Scripts\activate.bat
echo.
echo ====================================================
echo   لوحة تحكم - قناة الشيخ سامي العربي
echo   http://127.0.0.1:8000/
echo ====================================================
echo.
uvicorn src.dashboard_app:app --reload --host 127.0.0.1 --port 8000
