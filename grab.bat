@echo off
cd /d "%~dp0"
title Auto-Edit Studio
".venv\Scripts\python.exe" tools\studio\server.py
pause
