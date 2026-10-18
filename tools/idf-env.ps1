# ESP-IDF 环境统一入口（Lead 维护，dot-source 使用）
#   Set-ExecutionPolicy -Scope Process Bypass -Force   # ← 本机策略禁止 dot-source .ps1，必须先放开当前进程
#   . .\tools\idf-env.ps1
# 然后即可使用 idf.py / esptool.py / riscv32-esp-elf-gcc。
# 说明：本机 PATH 上没有真 python，也没有 git，这里统一注入 DSH 自带 python 与 Git for Windows。
# CMD/PowerShell 若不想 dot-source，可用 tools\idf.cmd（走官方 export.bat）。

$ErrorActionPreference = 'Stop'

$IdfRoot   = 'D:\esp'
$IdfVer    = 'idf-full\esp-idf-v5.5.6'   # 官方完整包（含 .git + 全部 submodule）；GitHub 自动 tarball 不含 submodule，不可用
$GitCands  = @('C:\Program Files\Git\cmd', 'D:\esp\PortableGit\bin')
$PyCands   = @("$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python", 'C:\Python312', 'C:\Python311')

$env:IDF_PATH             = Join-Path $IdfRoot $IdfVer
$env:IDF_TOOLS_PATH       = Join-Path $IdfRoot '.espressif'
$env:IDF_PYTHON_ENV_PATH  = Join-Path $IdfRoot 'python_env'
# 国内镜像：把 github.com 下载改走 dl.espressif.com（工具链/组件下载更稳）
if (-not $env:IDF_GITHUB_ASSETS) { $env:IDF_GITHUB_ASSETS = 'dl.espressif.com/github_assets' }
# 只装了 esp32p4 的工具链；不钉 IDF_TARGET 的话 export 会去校验 xtensa 等全部工具并失败
if (-not $env:IDF_TARGET) { $env:IDF_TARGET = 'esp32p4' }

if (-not (Test-Path $env:IDF_PATH)) { throw "IDF_PATH 不存在: $env:IDF_PATH" }

# PATH 顺序很关键：IDF venv 的 python 必须排在 DSH python 前面，
# 否则 idf.py 会用 DSH 解释器跑并报 "No module named 'click'"。
$prefix = @()
$envScripts = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts'
if (Test-Path (Join-Path $envScripts 'python.exe')) { $prefix += $envScripts }
foreach ($g in $GitCands) { if (Test-Path (Join-Path $g 'git.exe')) { $prefix += $g } }
foreach ($p in $PyCands)  { if (Test-Path (Join-Path $p 'python.exe')) { $prefix += $p; $prefix += (Join-Path $p 'Scripts') } }

$env:PATH = (($prefix | Select-Object -Unique) + $env:PATH) -join ';'

$exportPs1 = Join-Path $env:IDF_PATH 'export.ps1'
if (Test-Path $envScripts) {
    . $exportPs1
    Write-Host "[idf-env] OK  IDF=$env:IDF_PATH  tools=$env:IDF_TOOLS_PATH" -ForegroundColor Green
    $t = Get-Command idf.py -ErrorAction SilentlyContinue
    if ($t) { Write-Host "[idf-env] idf.py -> $($t.Source)" -ForegroundColor Green } else { Write-Host "[idf-env] 警告: PATH 上找不到 idf.py" -ForegroundColor Yellow }
} else {
    Write-Warning "[idf-env] python_env 还不存在（工具链未安装完），只注入了 python/git"
    Write-Host "[idf-env] 下一步: D:\esp\$IdfVer\install.bat esp32p4" -ForegroundColor Yellow
}