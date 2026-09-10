@echo off
rem Windows launcher for the console.
rem The probe rejects a foreign "core" package from site-packages; if it
rem fails, the source tree is prepended to the module search path.
set "MANTRA_LAUNCH_DIR=%~dp0"
python -c "import os, sys, core, core.console; sys.exit(0 if os.path.abspath(core.__file__).lower().startswith(os.environ['MANTRA_LAUNCH_DIR'].lower()) else 1)" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set "PYTHONPATH=%~dp0;%PYTHONPATH%"
    python -m core.console %*
) else (
    python -m core.console %*
)
