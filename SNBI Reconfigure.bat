@echo off
echo Use this to update your API key or bridge folder path.
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0setup_and_start.ps1" -Reconfigure
pause
