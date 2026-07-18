@echo off
setlocal

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

if not exist logs mkdir logs

python src\main.py digest --config routes.json --db data\prices.sqlite --env .env --hours 12 --notify >> logs\digest.log 2>>&1
echo [%date% %time%] exit_code=%errorlevel% >> logs\digest.log

endlocal
