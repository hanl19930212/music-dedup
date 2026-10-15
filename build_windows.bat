@echo off
chcp 65001 >nul
REM music-dedup Windows 打包脚本
REM 用法：把 ffmpeg.exe / fpcalc.exe 放进 bin\ 目录，然后在项目根执行本脚本
setlocal

echo === 1. 检查外部工具 ===
if not exist "%~dp0bin\fpcalc.exe" echo [警告] 缺少 bin\fpcalc.exe，运行时会报错
if not exist "%~dp0bin\ffmpeg.exe" echo [警告] 缺少 bin\ffmpeg.exe，fpcalc 可能无法解码

echo.
echo === 2. 安装依赖 ===
pip install pyinstaller
pip install -e "%~dp0"

echo.
echo === 3. 打包 ===
pyinstaller --clean --noconfirm "%~dp0packaging\music-dedup.spec"
if errorlevel 1 (
    echo [错误] 打包失败
    exit /b 1
)

echo.
echo === 完成 ===
echo 产物：%~dp0dist\music-dedup.exe
echo 直接运行该 exe 即可，无需安装 Python / ffmpeg / fpcalc
endlocal
