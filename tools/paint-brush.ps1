# tools/paint-brush.ps1 - select Paint brush type and set size / opacity via UI Automation
# usage: pwsh -NoProfile -File tools\paint-brush.ps1 -Size 6 [-Opacity 100] [-Brush Brush|Marker|Crayon|OilBrush|WaterColor|AirBrush|NaturalPencil]
param(
    [int]$Size = 6,
    [int]$Opacity = 100,
    [string]$Brush = "Brush"
)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$proc = Get-Process mspaint -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $proc) { Write-Output '{"ok":false,"err":"mspaint not running"}'; exit 1 }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)

function Find-ByType($type) {
    $c = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $type)
    $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $c)
}
function Find-ById($id) {
    $c = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
    $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
}

# 1) 展开「画笔」下拉，选一个笔形（它同时会把当前工具切成画笔）
$split = Find-ById "BrushesSplitButton"
if (-not $split) { Write-Output '{"ok":false,"err":"no BrushesSplitButton"}'; exit 1 }
try {
    $split.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
} catch {}
Start-Sleep -Milliseconds 500

$item = Find-ById $Brush
if (-not $item) {                       # 下拉没展开时退回点按钮本身
    try { $split.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
    Start-Sleep -Milliseconds 500
    $item = Find-ById $Brush
}
if ($item) {
    try { $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
    catch { try { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() } catch {} }
    Start-Sleep -Milliseconds 400
}

# 2) 设大小 / 不透明度
$result = @{ ok = $true; brush = $Brush; size = $null; opacity = $null }
foreach ($s in (Find-ByType ([System.Windows.Automation.ControlType]::Slider))) {
    $rv = $null
    try { $rv = $s.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern) } catch { continue }
    if ($s.Current.Name -eq '大小') { $rv.SetValue([double]$Size); Start-Sleep -Milliseconds 120; $result.size = $rv.Current.Value }
    if ($s.Current.Name -eq '不透明度') { $rv.SetValue([double]$Opacity); Start-Sleep -Milliseconds 120; $result.opacity = $rv.Current.Value }
}
$canvas = Find-ById "image"
if ($canvas) { $result.tool = $canvas.Current.Name }
Write-Output ($result | ConvertTo-Json -Compress)
