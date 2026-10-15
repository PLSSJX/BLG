@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 优先使用打包好的独立 exe（不需要装 Python）
if exist "%~dp0dist\BiliGrab\BiliGrab.exe" (
  "%~dp0dist\BiliGrab\BiliGrab.exe" %*
  goto :end
)
if exist "%~dp0dist\BiliGrab.exe" (
  "%~dp0dist\BiliGrab.exe" %*
  goto :end
)
if exist "%~dp0BiliGrab.exe" (
  "%~dp0BiliGrab.exe" %*
  goto :end
)

rem 没有 exe 就退回源码运行
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY (
  echo.
  echo   [x] 没有找到 BiliGrab.exe，也没有找到 Python。
  echo       请运行 build.py 打包出 exe，或安装 Python 3.9+ 后重试。
  echo.
  pause
  exit /b 1
)

echo.
echo   ============================================
echo      哔哩采集器 BiliGrab  ·  正在启动
echo   ============================================
echo.
%PY% "%~dp0start.py" serve %*
if errorlevel 1 pause

:end
endlocal
