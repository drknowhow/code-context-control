@echo off
setlocal EnableDelayedExpansion

:: ─── ANSI colours (Windows 10+) ─────────────────────────────────────────────
for /F %%a in ('echo prompt $E^| cmd /Q /D') do set "ESC=%%a"
set "R=%ESC%[0m"
set "BOLD=%ESC%[1m"
set "DIM=%ESC%[2m"
set "RED=%ESC%[91m"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "CYAN=%ESC%[96m"
set "WHITE=%ESC%[97m"
set "OK=%GREEN% OK %R%"
set "WARN=%YELLOW% !! %R%"
set "FAIL=%RED%FAIL%R%"

:: ─── Resolve C3 source directory ─────────────────────────────────────────────
set "C3_HOME=%~dp0"
if "%C3_HOME:~-1%"=="\" set "C3_HOME=%C3_HOME:~0,-1%"

:: ─── Read version from source ────────────────────────────────────────────────
for /f "tokens=2 delims==" %%v in ('findstr "__version__ =" "%C3_HOME%\cli\c3.py"') do (
    set "RAW_VER=%%v"
    set "C3_VER=!RAW_VER:"=!"
    set "C3_VER=!C3_VER: =!"
)

:: ─── Banner ───────────────────────────────────────────────────────────────────
cls
echo.
echo   %CYAN%================================================================%R%
echo   %BOLD%%WHITE%    C3  -  Claude Code Companion%R%   %DIM%v!C3_VER!%R%
echo   %DIM%    Windows Installer%R%
echo   %CYAN%================================================================%R%
echo.

:: ─── Check Python ────────────────────────────────────────────────────────────
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   %RED%  Python not found.%R%
    echo.
    echo   Install Python from %CYAN%https://python.org%R%
    echo   Check %YELLOW%"Add Python to PATH"%R% during installation.
    echo.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"

:: ─── Detect existing installation ────────────────────────────────────────────
set "INSTALLED=0"
set "INSTALLED_PATH="
for /f "tokens=*" %%p in ('where c3 2^>nul') do (
    if "!INSTALLED!"=="0" set "INSTALLED_PATH=%%p"
    set "INSTALLED=1"
)
if exist "%USERPROFILE%\.c3\hub_config.json" set "INSTALLED=1"

:: ─── Mode selection ───────────────────────────────────────────────────────────
if "!INSTALLED!"=="1" (
    echo   %YELLOW%  C3 appears to be already installed on this machine.%R%
    if defined INSTALLED_PATH (
        echo   %DIM%  Location  : !INSTALLED_PATH!%R%
    )
    echo.
    echo   %WHITE%  What would you like to do?%R%
    echo.
    echo   %CYAN%  [1]%R%  Update  - reinstall and upgrade to %BOLD%v!C3_VER!%R%
    echo   %CYAN%  [2]%R%  Remove  - uninstall C3 from this machine
    echo   %CYAN%  [3]%R%  Cancel
    echo.
    set /p "MODE=    Your choice [1/2/3]: "
    echo.
    if "!MODE!"=="3" goto :cancelled
    if "!MODE!"=="2" goto :do_remove
    echo   %CYAN%  Updating C3 to v!C3_VER!...%R%
    echo.
) else (
    echo   %DIM%  Python : !PY_VER!%R%
    echo   %DIM%  Source : %C3_HOME%%R%
    echo.
    set /p "CONFIRM=  Install C3 v!C3_VER!? [Y/n]: "
    echo.
    if /i "!CONFIRM!"=="n" goto :cancelled
    echo   %CYAN%  Installing C3 v!C3_VER!...%R%
    echo.
)

:: ─── [1/5] Install C3 package ────────────────────────────────────────────────
echo   %BOLD%[1/5]%R%  Installing C3 package + dependencies...
python -m pip install "%C3_HOME%[tui]" -q 2>nul
if !ERRORLEVEL! neq 0 (
    echo         Retrying with --user flag...
    python -m pip install --user "%C3_HOME%[tui]" -q 2>nul
)
if !ERRORLEVEL! equ 0 (echo         !OK!  C3 v!C3_VER! installed (entry-points: c3, c3-mcp, c3-hub^)) else (echo         !WARN!  pip install failed — check Python/pip and try manually:  python -m pip install "%C3_HOME%[tui]")
echo.

:: ─── [2/5] Create c3 command ──────────────────────────────────────────────────
echo   %BOLD%[2/5]%R%  Creating c3 command...

for /f "tokens=*" %%p in ('python -c "import sys; print(sys.prefix)" 2^>^&1') do set "PYTHON_DIR=%%p"
set "SCRIPTS_DIR=%PYTHON_DIR%\Scripts"

if exist "%SCRIPTS_DIR%" (
    set "WRAPPER=%SCRIPTS_DIR%\c3.bat"
) else (
    set "WRAPPER=%USERPROFILE%\.local\bin\c3.bat"
    if not exist "%USERPROFILE%\.local\bin" mkdir "%USERPROFILE%\.local\bin"
)

(
    echo @echo off
    echo set "C3_HOME=%C3_HOME%"
    echo set "PYTHONPATH=%%C3_HOME%%"
    echo if "%%~1"=="" ^(
    echo     python "%%C3_HOME%%\tui\main.py"
    echo ^) else ^(
    echo     python "%%C3_HOME%%\cli\c3.py" %%*
    echo ^)
) > "!WRAPPER!"
echo         !OK!  System wrapper : !WRAPPER!

(
    echo @echo off
    echo set "C3_WRAPPER_HOME=%%~dp0"
    echo set "PYTHONPATH=%%C3_WRAPPER_HOME%%"
    echo if "%%~1"=="" ^(
    echo     python "%%C3_WRAPPER_HOME%%tui\main.py"
    echo ^) else ^(
    echo     python "%%C3_WRAPPER_HOME%%\cli\c3.py" %%*
    echo ^)
) > "%C3_HOME%\c3.bat"
echo         !OK!  Local  backup  : %C3_HOME%\c3.bat
echo.

:: ─── [3/5] Initialize global ~/.c3 directory ─────────────────────────────────
echo   %BOLD%[3/5]%R%  Initializing C3 data directory...

set "C3_DATA=%USERPROFILE%\.c3"
if not exist "%C3_DATA%" (
    mkdir "%C3_DATA%"
    echo         !OK!  Created : %C3_DATA%
) else (
    echo         %DIM%  Exists  : %C3_DATA%%R%
)

set "HUB_CFG=%C3_DATA%\hub_config.json"
if not exist "%HUB_CFG%" (
    (
        echo {
        echo   "port": 3330,
        echo   "auto_open_browser": true
        echo }
    ) > "%HUB_CFG%"
    echo         !OK!  Created : %HUB_CFG%
) else (
    echo         %DIM%  Exists  : %HUB_CFG%%R%
)

set "PROJ_FILE=%C3_DATA%\projects.json"
if not exist "%PROJ_FILE%" (
    (echo {"projects": []}) > "%PROJ_FILE%"
    echo         !OK!  Created : %PROJ_FILE%
) else (
    echo         %DIM%  Exists  : %PROJ_FILE%%R%
)
echo.

:: ─── [4/5] Background service check ──────────────────────────────────────────
echo   %BOLD%[4/5]%R%  Checking background service prerequisites...

for /f "tokens=*" %%p in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>^&1') do set "PYTHONW=%%p"

if exist "!PYTHONW!" (
    echo         !OK!  pythonw.exe found — background hub service supported
) else (
    echo         !WARN!  pythonw.exe not found at: !PYTHONW!
    echo               Hub can still run but may show a console window.
    echo               Download the full Python installer from python.org.
)
echo.

:: ─── [5/5] Verify installation ─────────────────────────────────────────────
echo   %BOLD%[5/5]%R%  Verifying installation...
set "PYTHONPATH=%C3_HOME%"
set "FAIL_COUNT=0"

python "%C3_HOME%\cli\c3.py" --help >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  CLI ^(c3^)) else (echo         !WARN!  CLI — check Python path & set /a FAIL_COUNT+=1)

python -c "import textual" >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  TUI ^(Textual^)) else (echo         !WARN!  Textual not found   — pip install textual & set /a FAIL_COUNT+=1)

python -c "import flask" >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  Web UI ^(Flask^)) else (echo         !WARN!  Flask not found     — pip install flask & set /a FAIL_COUNT+=1)

python -c "import sys; sys.path.insert(0,'%C3_HOME%'); from cli.hub_server import C3_VERSION" >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  Project Hub) else (echo         !WARN!  Project Hub import failed & set /a FAIL_COUNT+=1)

python -c "import sys; sys.path.insert(0,'%C3_HOME%'); from services.project_manager import ProjectManager" >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  Project Manager) else (echo         !WARN!  ProjectManager import failed & set /a FAIL_COUNT+=1)

python -c "import sys; sys.path.insert(0,'%C3_HOME%'); from services.hub_service import HubService" >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  Hub Service) else (echo         !WARN!  HubService import failed & set /a FAIL_COUNT+=1)

python -c "import sys; sys.path.insert(0,'%C3_HOME%'); import fastmcp" >nul 2>&1
if !ERRORLEVEL! equ 0 (echo         !OK!  MCP ^(FastMCP^)) else (echo         !WARN!  FastMCP not found   — pip install fastmcp & set /a FAIL_COUNT+=1)

echo.
echo   %CYAN%================================================================%R%
if "!FAIL_COUNT!"=="0" (
    echo   %BOLD%%GREEN%  Installation complete^!%R%  C3 v!C3_VER! is ready.
) else (
    echo   %BOLD%%YELLOW%  Done with !FAIL_COUNT! warning^(s^).%R%  Resolve the !WARN! items above.
)
echo   %CYAN%================================================================%R%

goto :show_help

:: ════════════════════════════════════════════════════════════════════════════
::   REMOVE
:: ════════════════════════════════════════════════════════════════════════════
:do_remove
echo   %BOLD%%RED%Removing C3...%R%
echo.

if defined INSTALLED_PATH (
    del /f "!INSTALLED_PATH!" >nul 2>&1
    echo   !OK!  Removed system wrapper : !INSTALLED_PATH!
) else (
    echo   %DIM%  No system wrapper found on PATH.%R%
)

if exist "%C3_HOME%\c3.bat" (
    del /f "%C3_HOME%\c3.bat" >nul 2>&1
    echo   !OK!  Removed local wrapper  : %C3_HOME%\c3.bat
)

echo.
if exist "%USERPROFILE%\.c3" (
    echo   %YELLOW%  Data directory: %USERPROFILE%\.c3%R%
    set /p "RMDATA=  Remove all C3 data? (sessions, memory, projects) [y/N]: "
    echo.
    if /i "!RMDATA!"=="y" (
        rmdir /s /q "%USERPROFILE%\.c3" >nul 2>&1
        echo   !OK!  Removed data directory : %USERPROFILE%\.c3
    ) else (
        echo   %DIM%  Data kept at: %USERPROFILE%\.c3%R%
    )
)

echo.
echo   %CYAN%================================================================%R%
echo   %BOLD%%GREEN%  C3 removed successfully.%R%
echo   %CYAN%================================================================%R%
echo.
goto :done

:: ════════════════════════════════════════════════════════════════════════════
::   CANCELLED
:: ════════════════════════════════════════════════════════════════════════════
:cancelled
echo   %DIM%  Cancelled.%R%
echo.
goto :done

:: ════════════════════════════════════════════════════════════════════════════
::   COMMAND REFERENCE
:: ════════════════════════════════════════════════════════════════════════════
:show_help
echo.
echo   %BOLD%GETTING STARTED%R%
echo   ----------------------------------------------------------------
echo   %CYAN%  c3 init .%R%              Initialize C3 for a project
echo   %CYAN%  c3 install-mcp%R%         Wire MCP into your IDE
echo   %CYAN%  c3 ui%R%                  Per-project web dashboard
echo   %CYAN%  c3 hub%R%                 Global project hub  ^(port 3330^)
echo   %CYAN%  c3%R%                     Interactive TUI
echo.
echo   %BOLD%ALL COMMANDS%R%
echo   ----------------------------------------------------------------
echo     c3                       Open interactive TUI
echo     c3 init .                Initialize / repair C3 for current project
echo     c3 install-mcp           Configure MCP for your IDE
echo     c3 permissions           Show or apply a Claude Code permission tier
echo     c3 ui                    Launch per-project web dashboard
echo     c3 ui --nano             Lightweight mission-control panel
echo     c3 hub                   Launch Project Hub web UI  ^(port 3330^)
echo     c3 hub --no-browser      Start hub silently in background
echo     c3 hub --install         Register hub as Windows startup task
echo     c3 hub --uninstall       Remove hub startup task
echo     c3 hub --status          Show hub background service status
echo     c3 projects list         List all registered projects
echo     c3 projects add .        Register current directory
echo     c3 stats                 Show session and token stats
echo     c3 benchmark             Run local token-efficiency benchmark
echo.
echo   %BOLD%BACKGROUND SERVICE%R%
echo   ----------------------------------------------------------------
echo   From the Project Hub, open %YELLOW%Settings ^> Install Service%R% to register
echo   the hub as a Windows startup task — runs automatically on login.
echo.
echo   %DIM%  Hub config : %USERPROFILE%\.c3\hub_config.json%R%
echo   %DIM%  Hub log    : %USERPROFILE%\.c3\hub.log%R%
echo.
echo   %BOLD%NOTE%R%
echo   ----------------------------------------------------------------
echo   If 'c3' is not recognized after install, open a new terminal or use:
echo   %DIM%  %C3_HOME%\c3.bat%R%
echo.

:done
pause
exit /b 0
