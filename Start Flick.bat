@echo off
rem Starts Flick hidden (tray icon near the clock) and opens the laptop page.
cd /d "%~dp0"
start "" pythonw server.py --background
