@echo off
cd /d "%~dp0"
python viewer.py
if errorlevel 1 pause
