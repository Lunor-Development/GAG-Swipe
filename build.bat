@echo off
setlocal
cd /d "%~dp0"

echo Installing build deps...
py -3 -m pip install -r requirements.txt pyinstaller pywin32-ctypes -q --upgrade

echo Building gag-swipe.exe ...
py -3 -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --console ^
  --name gag-swipe ^
  --collect-submodules httpx ^
  --hidden-import proxy_pool ^
  --hidden-import reauth ^
  --hidden-import gag_client ^
  --hidden-import config_util ^
  auto_swipe.py

if %ERRORLEVEL% neq 0 (
  echo Build failed.
  exit /b 1
)

echo.
echo Done: dist\gag-swipe.exe
echo Double-click to run — paste gag_session and .ROBLOSECURITY when prompted.
endlocal
