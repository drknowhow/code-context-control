@echo off
set "C3_WRAPPER_HOME=%~dp0"
set "PYTHONPATH=%C3_WRAPPER_HOME%"
if "%~1"=="" (
    python "%C3_WRAPPER_HOME%tui\main.py"
) else (
    python "%C3_WRAPPER_HOME%\cli\c3.py" %*
)
