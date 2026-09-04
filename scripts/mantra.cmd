@echo off
rem Launcher: starts the interactive harness from any directory.
rem The caller's current folder becomes the agent workspace.
rem On failure, retry once with the source tree on PYTHONPATH so an
rem uninstalled checkout still runs.
python -m mantra.console %*
if %ERRORLEVEL% NEQ 0 set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
if %ERRORLEVEL% NEQ 0 python -m mantra.console %*
