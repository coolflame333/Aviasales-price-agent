@echo off
setlocal

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

if not exist logs mkdir logs

set "PLAYWRIGHT_BROWSERS_PATH=browsers\ms-playwright"

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe src\main.py live-check --live-config live_searches.json --db data\prices.sqlite --env .env --notify --no-alert-exit-code >> logs\live.log 2>>&1
) else (
    python src\main.py live-check --live-config live_searches.json --db data\prices.sqlite --env .env --notify --no-alert-exit-code >> logs\live.log 2>>&1
)
echo [%date% %time%] exit_code=%errorlevel% >> logs\live.log

endlocal
