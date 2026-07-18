@echo off
setlocal

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

if not exist logs mkdir logs

python src\main.py status --config routes.json --db data\prices.sqlite --env .env --stale-minutes 90 --notify >> logs\status.log 2>>&1
echo [%date% %time%] exit_code=%errorlevel% >> logs\status.log

endlocal
