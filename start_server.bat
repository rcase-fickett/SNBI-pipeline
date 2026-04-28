@echo off
:: SNBI Review App — Network Server
:: Run this on the shared machine. Team members open their browser to the URL printed below.

cd /d "%~dp0"

:: Set the Anthropic API key (paste your key here or set it as a Windows environment variable)
:: If ANTHROPIC_API_KEY is already set as a system/user environment variable, leave this line commented out.
:: set ANTHROPIC_API_KEY=sk-ant-YOUR-KEY-HERE

echo Starting SNBI Review App (network mode)...
python app.py --port 5000

pause
