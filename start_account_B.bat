@echo off
setlocal
chcp 65001 >nul
set "CLIENT_EXE=%~dp0dist\MailForwarderClient.exe"
if not exist "%CLIENT_EXE%" set "CLIENT_EXE=%~dp0MailForwarderClient.exe"
if not exist "%CLIENT_EXE%" goto missing_exe
set "CLIENT_BASE=%LOCALAPPDATA%\MailForwarderClient"
if not defined LOCALAPPDATA set "CLIENT_BASE=%USERPROFILE%\AppData\Local\MailForwarderClient"
set "CLIENT_DATA=%CLIENT_BASE%\instances\%~n0"
if /I "%~n0"=="start_account_A" set "CLIENT_DATA=%CLIENT_BASE%"
echo Starting instance: "%~n0"
echo Data folder: "%CLIENT_DATA%"
start "" "%CLIENT_EXE%" --data-dir "%CLIENT_DATA%"
if errorlevel 1 goto launch_failed
exit /b 0

:missing_exe
echo MailForwarderClient.exe was not found. Run build_windows.bat first.
pause
exit /b 1

:launch_failed
echo The client could not be started.
pause
exit /b 1
