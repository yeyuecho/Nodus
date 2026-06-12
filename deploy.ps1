<#
.SYNOPSIS
    柒月·合一 VM 部署脚本
.DESCRIPTION
    将 F:\qiyue-heyi 同步到 Hyper-V VM。
    处理目录扁平化问题（逐个文件传输）。
#>

param(
    [string]$VMName = "Windows 10 MSIX packaging environment",
    [string]$VMUser = "admin",
    [string]$VMPass = "bo551830",
    [string]$SourcePath = "F:\qiyue-heyi",
    [string]$DestPath = "C:\Users\admin\qiyue-heyi"
)

$ErrorActionPreference = "Stop"

# 建立 VM 会话
$cred = New-Object System.Management.Automation.PSCredential($VMUser, (ConvertTo-SecureString $VMPass -AsPlainText -Force))
$session = New-PSSession -VMName $VMName -Credential $cred

# 清理 VM 上可能存在的脑文件扁平化残留
Invoke-Command -Session $session -ScriptBlock {
    param($root)
    # 清理散落在根目录的 brain 文件
    @('__init__.py', 'cron.py', 'dream.py', 'llm_loop.py', 'planning.py', 'SOUL.md', 'AGENTS.md') | ForEach-Object {
        Remove-Item "$root\$_" -ErrorAction SilentlyContinue
    }
    @('prompts', '__pycache__') | ForEach-Object {
        Remove-Item "$root\$_" -Recurse -Force -ErrorAction SilentlyContinue
    }
} -ArgumentList $DestPath

# 获取所有需要同步的文件（排除 venv、.env、__pycache__）
$files = Get-ChildItem -Path $SourcePath -Recurse -File |
    Where-Object {
        $_.FullName -notmatch '\\venv\\' -and
        $_.FullName -notmatch '\\__pycache__\\' -and
        $_.Name -ne '.env' -and
        $_.Name -notmatch '\.pyc$'
    }

Write-Host "Syncing $($files.Count) files..."

$ok = 0
$fail = 0
foreach ($f in $files) {
    $relPath = $f.FullName.Substring($SourcePath.Length + 1)
    $dstPath = Join-Path $DestPath $relPath

    # 确保目标目录存在
    $dstDir = Split-Path $dstPath -Parent
    Invoke-Command -Session $session -ScriptBlock {
        param($dir)
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    } -ArgumentList $dstDir

    try {
        Copy-Item $f.FullName -Destination $dstPath -ToSession $session -Force -ErrorAction Stop
        $ok++
    } catch {
        Write-Host "  FAIL: $relPath - $_"
        $fail++
    }
}

Remove-PSSession $session

Write-Host ""
Write-Host "Synced: $ok OK, $fail FAIL"
if ($fail -gt 0) {
    exit 1
}
