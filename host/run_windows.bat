@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv .venv || goto :error
    ) else (
        py -3 -m venv .venv || goto :error
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
    copy /Y "requirements.txt" ".venv\requirements.installed" >nul
)

rem Install new or changed dependencies (e.g. after an update of the bridge).
fc /b "requirements.txt" ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Updating Python dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
    copy /Y "requirements.txt" ".venv\requirements.installed" >nul
)

if not exist "config.json" (
    copy /Y "config.example.json" "config.json" >nul
    echo Created host\config.json from the example configuration.
)

".venv\Scripts\python.exe" control_deck_bridge.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo PC Control Deck bridge stopped with an error.
pause
exit /b 1
