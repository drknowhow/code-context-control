@echo off
REM Oracle launcher — start / stop / status.
REM Usage:
REM   oracle_start.bat           -> start Oracle (default, opens browser)
REM   oracle_start.bat start     -> start Oracle
REM   oracle_start.bat stop      -> stop running Oracle
REM   oracle_start.bat restart   -> stop then start
REM   oracle_start.bat status    -> show whether Oracle is running

setlocal EnableDelayedExpansion
set "ORACLE_HOME=%~dp0"
set "PYTHONPATH=%ORACLE_HOME%"
set "ORACLE_PORT=3331"
set "PID_FILE=%ORACLE_HOME%.oracle.pid"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if /I "%ACTION%"=="start"   goto :start
if /I "%ACTION%"=="stop"    goto :stop
if /I "%ACTION%"=="restart" goto :restart
if /I "%ACTION%"=="status"  goto :status

echo Unknown action: %ACTION%
echo Usage: oracle_start.bat [start^|stop^|restart^|status]
exit /b 1

:status
if exist "%PID_FILE%" (
    set /p RUNNING_PID=<"%PID_FILE%"
    tasklist /FI "PID eq !RUNNING_PID!" 2>nul | findstr /R /C:"^python" >nul
    if not errorlevel 1 (
        echo Oracle is running ^(PID !RUNNING_PID!^) on port %ORACLE_PORT%
        exit /b 0
    )
    del "%PID_FILE%" >nul 2>&1
)
netstat -ano | findstr ":%ORACLE_PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo Oracle port %ORACLE_PORT% is in use but PID file is missing.
    exit /b 0
)
echo Oracle is not running.
exit /b 1

:restart
call :stop
call :start
exit /b 0

:stop
set "STOPPED=0"
if exist "%PID_FILE%" (
    set /p RUNNING_PID=<"%PID_FILE%"
    echo Stopping Oracle ^(PID !RUNNING_PID!^)...
    taskkill /PID !RUNNING_PID! /T /F >nul 2>&1
    del "%PID_FILE%" >nul 2>&1
    set "STOPPED=1"
)
REM Fallback: kill anything listening on the oracle port
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%ORACLE_PORT% " ^| findstr "LISTENING"') do (
    echo Killing stray process on port %ORACLE_PORT% ^(PID %%P^)...
    taskkill /PID %%P /T /F >nul 2>&1
    set "STOPPED=1"
)
if "!STOPPED!"=="1" (
    echo Oracle stopped.
) else (
    echo Oracle was not running.
)
exit /b 0

:start
if exist "%PID_FILE%" (
    set /p RUNNING_PID=<"%PID_FILE%"
    tasklist /FI "PID eq !RUNNING_PID!" 2>nul | findstr /R /C:"^python" >nul
    if not errorlevel 1 (
        echo Oracle is already running ^(PID !RUNNING_PID!^) on port %ORACLE_PORT%
        exit /b 0
    )
    del "%PID_FILE%" >nul 2>&1
)
echo Starting Oracle on port %ORACLE_PORT%...
start "Oracle" /MIN cmd /c "python -m oracle.oracle_server --port %ORACLE_PORT% & echo stopped & pause"
REM Record the python PID (find most recently launched python.exe)
for /f "skip=1 tokens=2" %%P in ('wmic process where "name='python.exe'" get ProcessId /format:table 2^>nul') do (
    set "NEW_PID=%%P"
)
if defined NEW_PID (
    > "%PID_FILE%" echo !NEW_PID!
    echo Oracle started ^(PID !NEW_PID!^). Open http://localhost:%ORACLE_PORT%
) else (
    echo Oracle start issued. PID not captured ^(use 'oracle_start.bat stop' via port fallback^).
)
exit /b 0
