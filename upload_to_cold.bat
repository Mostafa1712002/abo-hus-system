@echo off
chcp 65001 >nul
setlocal

set PROJECT_DIR=%~dp0
set PYTHON=%PROJECT_DIR%venv\Scripts\python.exe
set SCRIPT=%PROJECT_DIR%upload_to_cold.py
set PYTHONIOENCODING=utf-8

echo ============================================================
echo  Bulk upload to cold storage
echo ------------------------------------------------------------
echo  Source : E:\فضيلة الشيخ أبي حفص\مرئيات
echo  Dest   : abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/
echo  Files  : ~1738 video files, ~220 GB
echo ------------------------------------------------------------
echo  This may take several hours depending on upload speed.
echo  The transfer is RESUMABLE — Ctrl+C and re-run safely.
echo  Already-uploaded files (matching size) are skipped.
echo ============================================================
echo.

if not exist "%PYTHON%" (
  echo ERROR: Python venv not found at %PYTHON%
  pause
  exit /b 1
)
if not exist "%SCRIPT%" (
  echo ERROR: upload_to_cold.py not found at %SCRIPT%
  pause
  exit /b 1
)

pause

"%PYTHON%" "%SCRIPT%" %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
  echo === Upload completed successfully ===
) else (
  echo === Upload finished with exit code %RC% — re-run this script to resume ===
)
pause
endlocal
exit /b %RC%
