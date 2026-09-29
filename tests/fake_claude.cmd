@echo off
rem Mimics the claude.cmd shim an npm install puts on PATH: a batch file that starts the real CLI.
"%FAKE_PYTHON%" "%~dp0fake_claude.py" %*
