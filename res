#!/bin/bash
# `res` for the Macs: runs res.py in its venv (res.py itself explains how to create a missing venv).
# Usage: ~/ResilioSync/Services/LeaseResources/res now
VENV_PY="$HOME/venvs/LeaseResources/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="python3"
exec "$VENV_PY" -B "$(dirname "$0")/res.py" "$@"
