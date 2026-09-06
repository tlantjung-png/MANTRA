@echo off
rem Windows launcher for the console. Delegates to the interpreter module.
rem If the package is not installed, run it from this repository instead.
rem A probe distinguishes "module missing" from a genuine runtime failure,
rem so a failing run is not silently executed a second time. It also
rem rejects a foreign "core" package from site-packages: only a core that
rem resolves under this launcher's directory passes the probe, so a
rem different package cannot hijack the console.
set "MANTRA_LAUNCH_DIR=%~dp0"
python -c "import os, sys, core, core.console; sys.exit(0 if os.path.abspath(core.__file__).lower().startswith(os.environ['MANTRA_LAUNCH_DIR'].lower()) else 1)" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set "PYTHONPATH=%~dp0;%PYTHONPATH%"
    python -m core.console %*
) else (
    python -m core.console %*
)
