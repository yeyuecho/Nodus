# Nodus 一键安装脚本
# 用法: irm https://raw.githubusercontent.com/yeyuecho/Nodus/main/scripts/install.ps1 | iex

$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  灵枢 Nodus 安装向导" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Python
Write-Host "[1/4] 检查 Python..." -ForegroundColor Yellow
try {
    $py = python --version 2>&1
    Write-Host "  [OK] $py"
} catch {
    Write-Host "  [FAIL] 未找到 Python 3.11+" -ForegroundColor Red
    Write-Host "  请先安装: https://www.python.org/downloads/"
    exit 1
}

# 2. 下载
Write-Host "[2/4] 下载 Nodus..." -ForegroundColor Yellow
$target = "$env:USERPROFILE\Nodus"
if (Test-Path $target) {
    Write-Host "  [OK] 目录已存在，更新..."
    Push-Location $target
    git pull 2>&1 | Out-Null
} else {
    git clone https://github.com/yeyuecho/Nodus.git $target 2>&1 | Out-Null
    Push-Location $target
}

# 3. 安装
Write-Host "[3/4] 安装依赖..." -ForegroundColor Yellow
python -m pip install -e . -q 2>&1 | Out-Null
Write-Host "  [OK] nodus 命令已全局可用"

# 4. API Key
Write-Host "[4/4] 配置 API Key..." -ForegroundColor Yellow
$key = Read-Host "  请输入 DEEPSEEK_API_KEY（直接粘贴）"
if ($key) {
    "# 灵枢 Nodus`nDEEPSEEK_API_KEY=$key`nDEEPSEEK_MODEL=deepseek-v4-pro" | Out-File -FilePath ".env" -Encoding utf8
    Write-Host "  [OK] 已保存" -ForegroundColor Green
} else {
    Write-Host "  [WARN] 跳过" -ForegroundColor Yellow
}

Pop-Location

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  安装完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  nodus doctor   检查环境"
Write-Host "  nodus start    启动"
Write-Host "========================================" -ForegroundColor Cyan
