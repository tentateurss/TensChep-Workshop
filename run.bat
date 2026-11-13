@echo off
rem Starts the PoE crafting planner web UI. Builds the database on first run.
cd /d "%~dp0"
if not exist data\poecraft.db (
    echo First run: downloading game data from RePoE and building the database...
    python -m poecraft update || goto :error
)
python -m poecraft serve %*
goto :eof
:error
echo Data update failed. See README.md, section "Offline data import".
pause
