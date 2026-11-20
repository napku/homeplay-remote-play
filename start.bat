@echo off
setlocal EnableExtensions
title HomePlay
cd /d "%~dp0"

echo.
echo   HomePlay - play the games on this PC from your phone or another PC
echo.

rem ------------------------------------------------------------------ was a file removed by antivirus?
if not exist "%~dp0homeplay_host.py" (
  echo   The main program file "homeplay_host.py" is missing.
  echo   Windows Defender may have quarantined it. To get it back:
  echo     1. Open Windows Security ^> Virus ^& threat protection ^> Protection history
  echo        and allow / restore the HomePlay item.
  echo     2. Right-click "allow-in-defender.bat" ^> Run as administrator.
  echo     3. Run start.bat again.
  echo.
  pause
  exit /b 1
)

rem ------------------------------------------------------------------ Python
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo   Python is not installed yet.
  echo     1. Download it from https://www.python.org/downloads/
  echo     2. In the installer, tick "Add python.exe to PATH".
  echo     3. Run start.bat again.
  echo.
  pause
  exit /b 1
)

rem ------------------------------------------------------------------ private Python environment
if not exist ".venv\Scripts\python.exe" (
  echo   First start: setting things up. This takes a minute or two...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo   Could not create the Python environment in "%~dp0.venv".
    pause
    exit /b 1
  )
)
set "VPY=%~dp0.venv\Scripts\python.exe"

"%VPY%" -c "import numpy, soundcard" >nul 2>nul
if errorlevel 1 (
  echo   Installing sound support...
  "%VPY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
  if errorlevel 1 echo   Warning: sound support could not be installed. Streaming still works, just without sound.
)

"%VPY%" -c "import vgamepad" >nul 2>nul
if errorlevel 1 (
  echo   Installing controller support. If a "ViGEmBus" setup window opens, click through it to install the driver.
  "%VPY%" -m pip install --disable-pip-version-check --quiet vgamepad
  if errorlevel 1 echo   Warning: controller support could not be installed. Keyboard and mouse still work.
)

rem ------------------------------------------------------------------ FFmpeg (screen capture + video encoding)
"%VPY%" homeplay_host.py --check-ffmpeg >nul 2>nul
if errorlevel 1 (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo   FFmpeg is needed. Download "ffmpeg-release-essentials.zip" from
    echo   https://www.gyan.dev/ffmpeg/builds/ and unzip it into a folder named "ffmpeg" next to start.bat.
    echo.
    pause
    exit /b 1
  )
  echo   Installing FFmpeg with winget...
  winget install --id Gyan.FFmpeg.Essentials -e --source winget
  "%VPY%" homeplay_host.py --check-ffmpeg >nul 2>nul
  if errorlevel 1 (
    echo   FFmpeg was installed but can't be found yet. Close this window and run start.bat again.
    echo.
    pause
    exit /b 1
  )
)

rem ------------------------------------------------------------------ run
echo.
"%VPY%" homeplay_host.py --tailscale %*
echo.
pause
