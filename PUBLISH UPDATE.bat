@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Publish Quran Update
cls

echo ==================================================================
echo   PUBLISH QURAN UPDATE
echo ==================================================================
echo.
echo   This sends your database changes to everyone using the app.
echo   Make sure DB Browser for SQLite is CLOSED before continuing.
echo.

REM ---------------------------------------------------------------- Python
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY ( python3 --version >nul 2>&1 && set "PY=python3" )

if not defined PY (
  echo ==================================================================
  echo   SETUP NEEDED - Python is not installed
  echo ==================================================================
  echo.
  echo   This is a one-time installation.
  echo.
  echo     1. Go to  https://www.python.org/downloads/
  echo     2. Click the big yellow "Download Python" button
  echo     3. IMPORTANT: tick "Add python.exe to PATH" on the first screen
  echo     4. Click Install Now, then run this file again
  echo.
  goto :end
)

REM ------------------------------------------------------------------- Git
git --version >nul 2>&1
if errorlevel 1 (
  echo ==================================================================
  echo   SETUP NEEDED - Git is not installed
  echo ==================================================================
  echo.
  echo   This is a one-time installation.
  echo.
  echo     1. Go to  https://git-scm.com/download/win
  echo     2. Run the installer and accept every default option
  echo     3. Run this file again when it finishes
  echo.
  goto :end
)

REM ------------------------------------------------------- Folder is ready
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo ==================================================================
  echo   SETUP NEEDED - this folder is not connected to GitHub
  echo ==================================================================
  echo.
  echo   Send your developer this message:
  echo     "The publish folder is not a git repository."
  echo.
  goto :end
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo ==================================================================
  echo   SETUP NEEDED - no GitHub address is configured
  echo ==================================================================
  echo.
  echo   Send your developer this message:
  echo     "The publish folder has no git remote."
  echo.
  goto :end
)

REM ----------------------------------------------- 1. Check and build files
echo ------------------------------------------------------------------
echo   STEP 1 of 3 - Checking your changes
echo ------------------------------------------------------------------
echo.

%PY% "export.py"
set "CODE=!errorlevel!"

if "!CODE!"=="2" goto :end
if not "!CODE!"=="0" goto :end

REM ----------------------------------------------------- 2. Commit and push
echo ------------------------------------------------------------------
echo   STEP 2 of 3 - Uploading
echo ------------------------------------------------------------------
echo.

git add -A >nul 2>&1
if errorlevel 1 (
  echo   Could not prepare the files for upload.
  echo   Send the newest file in the "logs" folder to your developer.
  echo.
  goto :end
)

for /f "delims=" %%T in ('%PY% -c "import datetime;print(datetime.datetime.now().strftime('%%Y-%%m-%%d %%H:%%M'))"') do set "STAMP=%%T"

git commit -m "Content update - !STAMP!" >nul 2>&1
if errorlevel 1 (
  git diff --cached --quiet >nul 2>&1
  if not errorlevel 1 (
    echo   Everything was already uploaded. Nothing more to do.
    echo.
    goto :end
  )
  echo   Could not save the changes.
  echo.
  echo   Git may not know who you are yet. Send your developer this
  echo   message: "git commit failed, user identity may not be set."
  echo.
  goto :end
)

echo   Sending to GitHub...
git push >nul 2>&1
if errorlevel 1 (
  echo.
  echo   ================================================================
  echo     UPLOAD FAILED - your work is saved but not yet published
  echo   ================================================================
  echo.
  echo   Nothing was lost. The most likely reasons:
  echo.
  echo     - No internet connection. Reconnect and run this file again.
  echo.
  echo     - GitHub is asking you to sign in. A browser or sign-in
  echo       window may have opened behind this one - check your taskbar,
  echo       sign in, then run this file again.
  echo.
  echo   If it keeps failing, send the newest file in the "logs" folder
  echo   to your developer.
  echo.
  goto :end
)

echo   Uploaded successfully.
echo.

REM ------------------------------------------------------- 3. Refresh cache
echo ------------------------------------------------------------------
echo   STEP 3 of 3 - Notifying the app
echo ------------------------------------------------------------------
echo.

%PY% "export.py" --purge

echo.
echo ==================================================================
echo   PUBLISHED SUCCESSFULLY
echo ==================================================================
echo.
echo   Your changes are now live. Users will receive them the next
echo   time they open the app.
echo.
echo   You do NOT need to update the app on the Play Store.
echo.

:end
echo ------------------------------------------------------------------
echo   Press any key to close this window.
pause >nul
endlocal
