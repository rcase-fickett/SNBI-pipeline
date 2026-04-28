@echo off
:: SNBI Review App — Local only (not visible on network)

cd /d "%~dp0"

echo Starting SNBI Review App (local mode)...
python app.py --local --port 5000

pause
