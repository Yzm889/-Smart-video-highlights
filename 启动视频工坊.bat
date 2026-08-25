@echo off
REM ============================================================
REM  启动「春天短视频工坊」图形化 WebUI (Windows)
REM  双击即可启动本地服务并自动打开浏览器。
REM  若服务异常退出，会自动重启（最多 5 次）。
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"

if not defined PYEXE (
  if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
    set "PYEXE=%LOCALAPPDATA%\Python\bin\python.exe"
  ) else if exist "%USERPROFILE%\AppData\Local\Python\bin\python.exe" (
    set "PYEXE=%USERPROFILE%\AppData\Local\Python\bin\python.exe"
  )
)

if not defined PYEXE (
  echo [错误] 未找到 Python。请先安装 Python 并加入 PATH。
  pause
  exit /b 1
)

echo 正在启动「春天短视频工坊」...
echo 服务启动后会自动打开浏览器。关闭此窗口即停止服务。

set ATTEMPT=1
:loop
echo.
echo ---- 第 %ATTEMPT% 次启动服务 ----
"%PYEXE%" "webui_server.py"
set "RC=%ERRORLEVEL%"
echo.
echo 服务已停止（退出码 %RC%）。
if "%RC%"=="0" goto end
if %ATTEMPT% GEQ 5 goto end
echo 检测到异常退出，5 秒后自动重启...
choice /C SN /N /T 5 /D S /M "按 S 立即重启，按 N 退出？"
echo.
if errorlevel 2 goto end
set /A ATTEMPT+=1
goto loop
:end
pause
