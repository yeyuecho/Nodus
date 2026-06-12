@echo off
chcp 65001 >nul
echo.
echo ═══════════════════════════════════════
echo   柒月·合一 部署到 VM
echo ═══════════════════════════════════════
echo.

:: 检查 VM 是否在运行
powershell.exe -NoProfile -Command "$vm = Get-VM -Name 'Windows 10 MSIX packaging environment' -ErrorAction SilentlyContinue; if (-not $vm) { Write-Host '[ERROR] VM 不存在'; exit 1 }; if ($vm.State -ne 'Running') { Write-Host '[ERROR] VM 未运行，请先启动'; exit 1 }; Write-Host '[OK] VM 运行中'" 2>&1
if errorlevel 1 (
    pause
    exit /b 1
)

echo [部署] 同步文件到 VM...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" 2>&1
if errorlevel 1 (
    echo [ERROR] 部署失败
    pause
    exit /b 1
)

echo.
echo ═══════════════════════════════════════
echo   部署完成！
echo.
echo   在 VM 上操作：
echo     1. 打开 VM 桌面
echo     2. cd C:\Users\admin\qiyue-heyi
echo     3. setup.bat（首次）
echo     4. start.bat（启动）
echo ═══════════════════════════════════════
echo.
pause
