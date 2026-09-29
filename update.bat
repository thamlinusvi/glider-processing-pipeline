@echo off
:: ============================================================
:: update.bat — Pull latest code and restart
:: Run this whenever you want to apply updates from GitHub.
:: Close the app before running.
:: ============================================================

echo === Glider Processing Pipeline — Update ===
echo.

:: --- Pull latest code ---
echo [1/3] Pulling latest code from GitHub...
git pull
if errorlevel 1 (
    echo ERROR: git pull failed. Check your network or repo status.
    pause & exit /b 1
)

:: --- Node deps (only reinstalls if package.json changed) ---
echo.
echo [2/3] Checking Node dependencies...
cd /d "%~dp0electron_app"
call npm install
if errorlevel 1 (
    echo ERROR: npm install failed.
    pause & exit /b 1
)
cd /d "%~dp0"

:: --- Python deps (only installs/upgrades what changed) ---
echo.
echo [3/3] Checking Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause & exit /b 1
)

echo.
echo === Update complete. Launch the app: start_app.bat ===
echo.
pause
