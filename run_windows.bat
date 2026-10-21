@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python_fallback
py -3 -c "import tkinter; import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto python_fallback
set "CLIENT_PY=py -3"
goto run_client

:python_fallback
python -c "import tkinter; import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto missing_python
set "CLIENT_PY=python"
goto run_client

:run_client
if exist ".venv-build\Scripts\python.exe" goto check_ui
%CLIENT_PY% -m venv .venv-build
if errorlevel 1 goto failed
:check_ui
".venv-build\Scripts\python.exe" -c "import customtkinter; from PIL import Image" >nul 2>nul
if not errorlevel 1 goto launch
echo Installing desktop UI dependencies...
".venv-build\Scripts\python.exe" -X utf8 -m pip install -r requirements.txt
if errorlevel 1 goto failed
:launch
".venv-build\Scripts\python.exe" -X utf8 app.py
if errorlevel 1 goto failed
exit /b 0

:failed
echo Client launch failed. Please keep the error message above.
pause
exit /b 1

:missing_python
echo Install Python 3.10+ with Tcl/Tk or use the packaged MailForwarderClient.exe.
pause
exit /b 1
