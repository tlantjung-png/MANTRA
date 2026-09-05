@echo off
rem Windows launcher for the console. Delegates to the interpreter module.
rem If the package is not installed, retry once with the source tree on
rem PYTHONPATH. A probe distinguishes "module missing" from a genuine
rem runtime failure, so a failing run is not silently executed a second time.
python -c "import mantra" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
    python -m mantra.console %*
) else (
    python -m mantra.console %*
)
