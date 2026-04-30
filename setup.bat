@echo off
chcp 65001 >nul
echo ================================
echo   YouTube Auto Uploader - Setup
echo ================================
echo.

REM التأكد من Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [خطأ] Python مش متثبت.
    echo حمله من: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] إنشاء بيئة افتراضية...
python -m venv venv
if %errorlevel% neq 0 (
    echo فشل إنشاء البيئة الافتراضية
    pause
    exit /b 1
)

echo [2/4] تفعيل البيئة...
call venv\Scripts\activate.bat

echo [3/4] تحديث pip...
python -m pip install --upgrade pip

echo [4/4] تثبيت المكتبات...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo فشل تثبيت المكتبات
    pause
    exit /b 1
)

REM التأكد من وجود config.json
if not exist config.json (
    echo.
    echo نسخ config.example.json → config.json
    copy config.example.json config.json
    echo.
    echo [مهم] افتح config.json وحط مفتاح Gemini فيه.
)

echo.
echo ================================
echo   تم التثبيت بنجاح
echo ================================
echo.
echo الخطوات التالية:
echo   1. افتح config.json وحط مفتاح Gemini API
echo   2. حمّل client_secret.json من Google Cloud وحطه في credentials\
echo   3. تأكد إن ffmpeg متثبت ومتاح في PATH
echo   4. شغّل: run.bat
echo.
pause
