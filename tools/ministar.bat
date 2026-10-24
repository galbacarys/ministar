@echo off
setlocal

rem Windows entry point for ministar. See docs/research/00-windows-interop.md.
rem Invoked by tools/sync.sh after syncing the repo into C:\ministar\build.
rem NOTE: this file must keep CRLF line endings. With LF, cmd.exe misparses it
rem and silently loses the exit code (00 section 4).

set "MINISTAR_HOME=C:\ministar"

rem Repo root is the parent of this script's directory, fully qualified.
for %%I in ("%~dp0..") do set "ROOT=%%~fI"

set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo error: no venv at "%PY%" 1>&2
    echo        run tools\setup.bat once to create it 1>&2
    exit /b 2
)

set "PYTHONPATH=%ROOT%\src"
"%PY%" -m ministar %*
exit /b %errorlevel%
