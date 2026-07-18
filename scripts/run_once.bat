@echo off
setlocal

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

if not exist logs mkdir logs

python src\main.py run --config routes.json --db data\prices.sqlite --env .env --notify --no-alert-exit-code >> logs\scheduled.log 2>>&1
echo [%date% %time%] exit_code=%errorlevel% >> logs\scheduled.log

endlocal
