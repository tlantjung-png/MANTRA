@echo off
rem Windows launcher for the console. Delegates to the interpreter module.
rem On failure, retry once with the source tree on PYTHONPATH so an
rem uninstalled checkout still runs.
python -m mantra.console %*
if %ERRORLEVEL% NEQ 0 set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if %ERRORLEVEL% NEQ 0 python -m mantra.console %*
