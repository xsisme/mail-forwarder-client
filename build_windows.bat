@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Mail Forwarder Client - Windows EXE Builder
echo.
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys, tkinter; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto use_python
set "CLIENT_PY=py -3"
goto build

:use_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python -c "import sys, tkinter; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto missing_python
set "CLIENT_PY=python"

:build
echo [1/5] Creating isolated build environment...
%CLIENT_PY% -m venv .venv-build
if errorlevel 1 goto failed
echo [2/5] Installing desktop UI and build dependencies...
".venv-build\Scripts\python.exe" -X utf8 -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
echo [3/5] Running functional tests including Windows password encryption...
echo UI check windows close automatically. Please do not interact with them.
".venv-build\Scripts\python.exe" -X utf8 -m unittest discover -s tests -v
if errorlevel 1 goto failed
echo [4/5] Checking native desktop window without network access...
".venv-build\Scripts\python.exe" -X utf8 check_startup.py --data-dir .build-smoke
if errorlevel 1 goto failed
echo [5/5] Packaging standalone application...
".venv-build\Scripts\python.exe" -X utf8 -m PyInstaller --clean --noconfirm --onefile --windowed --collect-all customtkinter --collect-all darkdetect --name MailForwarderClient app.py
if errorlevel 1 goto failed
echo.
echo Checking the packaged EXE and bundled UI assets...
".venv-build\Scripts\python.exe" -X utf8 check_startup.py --exe "%CD%\dist\MailForwarderClient.exe" --data-dir "%CD%\.build-smoke-exe"
if errorlevel 1 goto failed
copy /Y "%CD%\start_account_A.bat" "%CD%\dist\start_account_A.bat" >nul
if errorlevel 1 goto failed
copy /Y "%CD%\start_account_B.bat" "%CD%\dist\start_account_B.bat" >nul
if errorlevel 1 goto failed
echo SUCCESS: "%CD%\dist\MailForwarderClient.exe"
echo Multiple accounts: use start_account_A.bat and start_account_B.bat in dist.
echo The generated EXE runs without Python installed.
explorer /select,"%CD%\dist\MailForwarderClient.exe"
pause
exit /b 0

:missing_python
echo Python 3.10+ with Tcl/Tk is required on the BUILD computer.
echo Install the standard Windows Python from https://www.python.org/downloads/windows/
echo Enable "Add Python to PATH" and "tcl/tk and IDLE", then run this file again.
pause
exit /b 1

:failed
echo.
echo Build failed before completion. Please copy the error text above.
echo Diagnostic folders: .build-test-logs, .build-smoke, .build-smoke-exe
pause
exit /b 1
