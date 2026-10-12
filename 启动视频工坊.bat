@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PYEXE=%LOCALAPPDATA%\Python\bin\python.exe"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)

if not defined PYEXE (
  echo [ERROR] Python not found.
  pause
  exit /b 1
)

echo Starting FrameCut WebUI ...
echo Browser opens automatically. Close this window to stop.

for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i ":8765 " ^| findstr LISTENING') do (
  taskkill /F /PID %%a >nul 2>nul
)

"%PYEXE%" "webui_server.py"

echo.
echo Server exited. Press any key to close.
pause >nul
