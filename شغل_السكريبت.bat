@echo off
chcp 65001 >nul
echo بدء جلب البيانات من السيرفر...
powershell -ExecutionPolicy Bypass -File "%~dp0fetch_from_server.ps1"
