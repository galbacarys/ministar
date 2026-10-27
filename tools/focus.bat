@echo off
setlocal

rem Launch the focus bench-test GUI on Windows.
rem Must keep CRLF line endings (00 section 4).

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo error: no venv at "%PY%" 1>^&2
    echo        run tools\setup.bat once to create it 1>^&2
    exit /b 2
)

set "PYTHONPATH=%ROOT%\src"
"%PY%" "%ROOT%\tools\focus_ui.py" %*
exit /b %errorlevel%
