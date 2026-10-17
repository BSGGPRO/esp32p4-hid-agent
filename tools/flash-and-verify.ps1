# flash-and-verify.ps1 — 一键：自动找 CH340 串口 → 烧录 → 协议自检 → 端到端验收
#
# 用法（PowerShell 7 / pwsh）：
#   pwsh -ExecutionPolicy Bypass -File tools\flash-and-verify.ps1
#   pwsh -ExecutionPolicy Bypass -File tools\flash-and-verify.ps1 -Port COM7
#   pwsh -ExecutionPolicy Bypass -File tools\flash-and-verify.ps1 -SkipFlash   # 只做验收
#
# 判定：任何一步失败即退出并打印失败原因（退出码非 0）。

[CmdletBinding()]
param(
    [string]$Port,
    [switch]$SkipFlash,
    [int]$Dx = 200
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # -> esp32p4-hid-agent
$fw   = Join-Path $root 'firmware'
$pc   = Join-Path $root 'pc'
$idf  = Join-Path $root 'tools\idf.cmd'
$PY   = 'C:\Users\lu''hao\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'

function Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }
function Ok($msg)   { Write-Host "[ OK ] $msg" -ForegroundColor Green }
function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------- 1. 找串口 ----------
Step '1/5 识别串口'
if (-not (Test-Path $PY)) { Fail "找不到 python: $PY" }

# 蓝牙虚拟串口（BthModem）不是开发板，排除掉
$btPorts = @()
$comm = Get-ItemProperty -Path 'HKLM:\HARDWARE\DEVICEMAP\SERIALCOMM' -ErrorAction SilentlyContinue
if ($comm) {
    foreach ($p in $comm.PSObject.Properties) {
        if ($p.Name -like '*BthModem*') { $btPorts += [string]$p.Value }
    }
}
$all = [System.IO.Ports.SerialPort]::GetPortNames()
Write-Host ("所有串口: " + ($all -join ', '))
if ($btPorts.Count) { Write-Host ("蓝牙虚拟口(排除): " + ($btPorts -join ', ')) }

if (-not $Port) {
    $cand = $all | Where-Object { $btPorts -notcontains $_ }
    if ($cand.Count -eq 0) {
        Fail '没有找到 CH340 串口。设备管理器里还是 "USB Serial" 吗？先以管理员运行 tools\install-ch340-admin.cmd'
    }
    if ($cand.Count -gt 1) {
        Fail ("有多个候选串口 (" + ($cand -join ', ') + ")，请用 -Port 指定，例如 -Port COM7")
    }
    $Port = $cand[0]
}
Ok "使用串口 $Port"

# ---------- 2. 烧录 ----------
if (-not $SkipFlash) {
    Step '2/5 烧录固件'
    Push-Location $fw
    try {
        & cmd /c "`"$idf`" -p $Port flash 2>&1" | Tee-Object -Variable flashOut | Select-Object -Last 25
        if ($LASTEXITCODE -ne 0) { Fail "烧录失败 (exit $LASTEXITCODE)。尝试断电重上电后再跑一次；仍失败则需手动 GPIO35 拉低进下载模式。" }
    } finally { Pop-Location }
    Ok '烧录完成'
} else {
    Step '2/5 跳过烧录 (-SkipFlash)'
}

# ---------- 3. 等固件起来并探活 ----------
Step '3/5 ping 探活'
Push-Location $pc
try {
    $pinged = $false
    for ($i = 1; $i -le 12; $i++) {
        $out = & $PY agent_bridge.py ping --port $Port 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0 -and $out -match '"pong"\s*:\s*true') { $pinged = $true; break }
        Write-Host ("  第 $i 次未通，等 1s 重试...") -ForegroundColor DarkGray
        Start-Sleep -Seconds 1
    }
    if (-not $pinged) { Write-Host $out; Fail 'ping 不通：检查波特率/是否烧的是本工程固件/串口是否被 monitor 占用' }
    Write-Host $out.Trim()
    Ok '固件在线'

    # ---------- 4. status ----------
    Step '4/5 status（host_mounted 必须为 true）'
    $st = & $PY agent_bridge.py status --port $Port 2>&1 | Out-String
    Write-Host $st.Trim()
    if ($st -notmatch '"host_mounted"\s*:\s*true') {
        Fail 'host_mounted=false：把【高速 Type-C】插到目标电脑（HID 走高速口，不是转 UART 口）'
    }
    Ok 'HID 已被主机枚举'

    # ---------- 5. 端到端 ----------
    Step "5/5 端到端验收（截屏 -> 鼠标右移 $Dx -> 再截屏 -> 差分）"
    & $PY agent_bridge.py e2e-demo --dx $Dx --port $Port 2>&1 | Out-String | Write-Host
    if ($LASTEXITCODE -ne 0) { Fail 'e2e-demo 失败' }
    Ok '端到端闭环通过'
} finally { Pop-Location }

Write-Host "`n全部通过：板子已在当键鼠用。" -ForegroundColor Green
Write-Host "后续常用：`n  $PY agent_bridge.py key-combo leftgui r --port $Port`n  $PY agent_bridge.py text `"hello`" --port $Port`n  $PY agent_bridge.py mouse-move 60 -20 --port $Port" -ForegroundColor DarkGray
exit 0
