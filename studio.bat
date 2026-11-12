@echo off
cd /d "%~dp0"
title VEdit Studio SHORT
".venv\Scripts\python.exe" tools\studio\server.py
pause
