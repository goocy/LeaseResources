@echo off
rem `res` for the Windows machines: runs res.py in its venv (res.py itself explains how to create a missing venv).
set "VENV_PY=%USERPROFILE%\venvs\LeaseResources\Scripts\python.exe"
if not exist "%VENV_PY%" set "VENV_PY=python"
"%VENV_PY%" -B "%~dp0res.py" %*
