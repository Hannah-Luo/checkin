@echo off
rem Double-click to start the check-in server and open the operator screen.
rem Closing this window stops the server.
cd /d "%~dp0"
title Lab Check-In server
set URL=http://127.0.0.1:8000/operator

rem Already running: only open the page. A second copy would fight it for the CPU.
powershell -NoProfile -Command "$c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 8000); exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
    echo The server is already running. Opening %URL%
    start "" %URL%
    timeout /t 3 >nul
    exit /b
)

rem Open the page as soon as the server accepts connections.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 120; $i++) { $c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 8000); Start-Process '%URL%'; exit } catch { Start-Sleep -Milliseconds 500 } }"

.venv\Scripts\python.exe -m app.server

echo.
echo The server has stopped. Press any key to close this window.
pause >nul
