@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE="
REM 优先使用本地安装的 Python（确保有 numpy 等依赖）
if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Python\bin\python.exe"
) else if exist "%USERPROFILE%\AppData\Local\Python\bin\python.exe" (
  set "PYEXE=%USERPROFILE%\AppData\Local\Python\bin\python.exe"
)
REM 回退到 PATH 中的 python
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
  echo [ERROR] Python not found. Please install Python 3 and add it to PATH, then run this script again.
  pause
  exit /b 1
)

echo Starting FrameCut WebUI ...
echo Browser opens automatically. Close this window to stop the server.

set ATTEMPT=1
:loop
echo.
echo ---- Start attempt %ATTEMPT% ----
REM Free port 8765 first so a stale process cannot serve old code
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i ":8765 " ^| findstr LISTENING') do (
  taskkill /F /PID %%a >nul 2>nul
)
"%PYEXE%" "webui_server.py"
set "RC=%ERRORLEVEL%"
echo.
echo Server stopped, exit code %RC%.
if "%RC%"=="0" goto end
if %ATTEMPT% GEQ 5 goto end
echo Abnormal exit detected, restart in 5s ...
choice /C SN /N /T 5 /D S /M "Press S to restart now, or N to quit."
echo.
if errorlevel 2 goto end
set /A ATTEMPT+=1
goto loop
:end
pause
