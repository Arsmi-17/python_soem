@echo off
title Motor Control System
cd /d "%~dp0app"

echo ============================================
echo    Motor Control System - Starting...
echo ============================================
echo.

:: Start the web server
echo Starting Python server...
start /min "Motor Web Server" python run_web.py

:: Wait for server to be ready (check if port 8000 is listening)
echo Waiting for server to start...
:waitloop
timeout /t 1 /nobreak > nul
powershell -Command "try { $null = (New-Object Net.Sockets.TcpClient).Connect('localhost', 8000); exit 0 } catch { exit 1 }"
if errorlevel 1 goto waitloop

:: Server is ready, open browser
echo Server started! Opening browser...
start "" "http://localhost:8000"

echo.
echo ============================================
echo   Server is running at http://localhost:8000
echo   Press any key to stop the server and exit
echo ============================================
echo.
pause > nul

:: Kill the Python process when user presses a key
taskkill /fi "WINDOWTITLE eq Motor Web Server*" /f > nul 2>&1
