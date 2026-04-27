@echo off
echo Building C3 TUI...
cd /d "%~dp0"
go mod tidy
go build -ldflags="-s -w" -o c3.exe .
if %ERRORLEVEL% equ 0 (
    echo Build successful: tui\c3.exe
) else (
    echo Build failed.
    exit /b 1
)
