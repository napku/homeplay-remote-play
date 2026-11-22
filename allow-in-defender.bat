@echo off
setlocal EnableExtensions
title HomePlay - trust this folder in Windows Defender
cd /d "%~dp0"

set "FOLDER=%~dp0"
if "%FOLDER:~-1%"=="\" set "FOLDER=%FOLDER:~0,-1%"

echo.
echo   This adds ONE folder to Windows Defender's exclusion list, so Defender
echo   stops scanning or quarantining HomePlay's files:
echo.
echo       "%FOLDER%"
echo.
echo   It does NOT turn your antivirus off. The rest of your PC stays protected.
echo   Only do this for software you trust and keep in this folder.
echo.

rem --- changing Defender settings needs an administrator window ---
net session >nul 2>nul
if errorlevel 1 (
  echo   Please run this as administrator:
  echo     right-click  allow-in-defender.bat  ^>  "Run as administrator"
  echo.
  pause
  exit /b 1
)

choice /c YN /m "Add this folder to the Windows Defender exclusions (Y/N)"
if errorlevel 2 (
  echo.
  echo   Cancelled. Nothing was changed.
  echo.
  pause
  exit /b 0
)

rem The folder goes to PowerShell in an environment variable and is never pasted into the
rem command text, so a quote or other special character in its name cannot change the command.
set "HP_FOLDER=%FOLDER%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Add-MpPreference -ExclusionPath $env:HP_FOLDER -ErrorAction Stop; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo.
  echo   Could not change Defender settings. Usual reasons:
  echo     - this PC is managed by an employer (your IT controls Defender), or
  echo     - Tamper Protection is on: Windows Security ^> Virus ^& threat
  echo       protection ^> Manage settings ^> turn off Tamper Protection, then
  echo       run this again, or add the folder by hand under "Exclusions".
  echo   Nothing was changed.
  echo.
  pause
  exit /b 1
)

echo.
echo   Done. Windows Defender will now leave the HomePlay folder alone.
echo.
echo   If Defender had already removed a file, restore it first:
echo     Windows Security ^> Virus ^& threat protection ^> Protection history,
echo   then run start.bat again.
echo.
echo   To undo this later, run as administrator:
echo     powershell Remove-MpPreference -ExclusionPath "%FOLDER%"
echo.
pause
