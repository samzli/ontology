@echo off
setlocal
REM ============================================================
REM  Onti - generic insurance ontology agent platform
REM  One-click start: creates the DB, seeds the sample pack and
REM  a default admin account on first run.
REM ============================================================
cd /d "%~dp0"

REM ---- Check dependencies WITHOUT importing them (handle-safe) -------------
python -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ['fastapi','uvicorn','pandas','openpyxl','reportlab','matplotlib','multipart']) else 1)" 2>nul
if errorlevel 1 goto :install
goto :portcheck

:install
echo Installing Python dependencies from requirements.txt ...
python -m pip install -r requirements.txt
if errorlevel 1 goto :pipfail

:portcheck
REM ---- Stop early if port 8010 is already in use ---------------------------
netstat -ano | findstr LISTENING | findstr ":8010" >nul 2>&1
if not errorlevel 1 goto :portbusy
goto :run

:portbusy
echo.
echo [ERROR] Port 8010 is already in use - another Onti server is running.
echo         Close that Onti window first, or end the Python process in
echo         Task Manager, then run this script again.
echo.
pause
exit /b 1

:run
echo ============================================================
echo   Onti is starting on http://localhost:8010
echo.
echo   User portal : http://localhost:8010/app
echo   Admin console: http://localhost:8010/admin
echo.
echo   Default admin  -  username: admin   password: Onti@2026
echo   Change it after first login.  Press Ctrl+C to stop.
echo ============================================================
python main.py
echo.
echo Server stopped.
pause
exit /b 0

:pipfail
echo.
echo Dependency installation failed. Check Python / pip and try again.
pause
exit /b 1
