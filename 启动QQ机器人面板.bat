@echo off
cd /d "%~dp0"
set "PY=%~dp0..\..\..\python\python.exe"
if exist "%PY%" goto run
set "PY=%~dp0python\python.exe"
if exist "%PY%" goto run
set "PY=python"
:run
"%PY%" "qq_bot_panel.py"
if errorlevel 1 pause
