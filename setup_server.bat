@echo off
:: ============================================================
:: setup_server.bat — First-time server setup
:: Run once after cloning the repo on a new machine.
:: Requirements: Python 3.10+, Node.js 24+, npm, Docker, Git
:: ============================================================

echo === Glider Processing Pipeline — Server Setup ===
echo.

:: --- 1. Node dependencies (Electron) ---
echo [1/4] Installing Node dependencies...
cd /d "%~dp0electron_app"
call npm install
if errorlevel 1 (
    echo ERROR: npm install failed. Is Node.js installed?
    pause & exit /b 1
)
cd /d "%~dp0"

:: --- 2. Python dependencies ---
echo.
echo [2/4] Installing Python dependencies...
echo NOTE: If cartopy or pyproj fail, install them via conda:
echo   conda install -c conda-forge cartopy pyproj contextily
echo   then re-run this script (those lines will just upgrade/skip)
echo.
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed. Check your Python environment.
    pause & exit /b 1
)

:: --- 3. Docker image ---
echo.
echo [3/4] Building glider-processing Docker image...
echo This may take several minutes on first run.
docker build -t glider-processing .
if errorlevel 1 (
    echo ERROR: Docker build failed. Is Docker Desktop running?
    pause & exit /b 1
)

:: --- 4. Data directories ---
echo.
echo [4/4] Creating data directories...
mkdir "%~dp0deployments"       2>nul
mkdir "%~dp0ad2cp"             2>nul
mkdir "%~dp0ad2cp\tests"       2>nul
mkdir "%~dp0incoming_data"     2>nul
mkdir "%~dp0dat_files"         2>nul
mkdir "%~dp0logs"              2>nul
mkdir "%~dp0config\cloud"      2>nul

echo.
echo === Setup complete ===
echo.
echo IMPORTANT — manual steps still required:
echo   1. Copy config\cloud\credentials.json from your existing machine
echo      (Google + Dropbox OAuth credentials — never stored in git)
echo   2. Copy config\cloud\tokens.json if you want to skip re-authenticating
echo   3. Copy any existing deployment data from deployments\, ad2cp\, etc.
echo   4. Launch the app: start_app.bat
echo.
pause
