@echo off
:: ============================================================
:: build_docker.bat — Build the glider-processing Docker image
:: Run this once after Docker Desktop is installed and running.
:: Also re-run after major pipeline updates that change the
:: Dockerfile or rugliderqc (baked into the image at build time).
:: ============================================================

echo === Building glider-processing Docker image ===
echo This may take several minutes on first run.
echo.

docker --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not on PATH.
    echo Make sure Docker Desktop is installed and running, then try again.
    pause
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker Engine is not running.
    echo Start Docker Desktop and wait for "Engine running", then try again.
    pause
    exit /b 1
)

docker build -t glider-processing .
if errorlevel 1 (
    echo.
    echo ERROR: Docker build failed. Check the output above for details.
    pause
    exit /b 1
)

echo.
echo === Docker image built successfully ===
echo You can now run all pipeline stages.
pause
