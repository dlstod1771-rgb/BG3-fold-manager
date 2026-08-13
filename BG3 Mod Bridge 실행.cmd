@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    wscript.exe //nologo "%~dp0launch-hidden.vbs"
    exit /b 0
)

rem Arguments such as --self-test intentionally keep a console for diagnostics.
where py.exe >nul 2>nul
if errorlevel 1 goto use_python
py.exe -3 "%~dp0BG3ModBridge.pyw" %*
goto check_result

:use_python
where python.exe >nul 2>nul
if errorlevel 1 goto no_python
python.exe "%~dp0BG3ModBridge.pyw" %*

:check_result
if errorlevel 1 goto failed
exit /b 0

:no_python
echo Python 3 was not found.
goto failed

:failed
echo.
echo BG3 Mod Bridge failed to start.
echo Error log: %LOCALAPPDATA%\BG3ModBridge\error.log
if exist "%LOCALAPPDATA%\BG3ModBridge\error.log" type "%LOCALAPPDATA%\BG3ModBridge\error.log"
pause
exit /b 1
