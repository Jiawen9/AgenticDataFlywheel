@echo off
cd /d "%~dp0"
python build_annotations.py
if errorlevel 1 pause
