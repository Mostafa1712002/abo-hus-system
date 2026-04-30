@echo off
chcp 65001 >nul
cd /d "C:\Users\aldaa\data\projects\youtube-auto-uploader"
venv\Scripts\python.exe transcode_rmvb.py --series "شرح الرسالة"
