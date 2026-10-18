# tools/paint-tool.ps1 - 通过 UI Automation 切换画图工具 / 缩放
# usage:
#   pwsh -NoProfile -File tools\paint-tool.ps1 -Tool pencil|fill|eraser|text
#   pwsh -NoProfile -File tools\paint-tool.ps1 -Zoom 100
param(
    [string]$Tool = "",
    [int]$Zoom = 0
)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PT {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, IntPtr e);
  public static void Click(int x, int y) {
    SetCursorPos(x, y); System.Threading.Thread.Sleep(140);
    mouse_event(0x0002, 0, 0, 0, IntPtr.Zero); System.Threading.Thread.Sleep(60);
    mouse_event(0x0004, 0, 0, 0, IntPtr.Zero);
  }
}
"@

$proc = Get-Process mspaint -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $proc) { Write-Output '{"ok":false,"err":"mspaint not running"}'; exit 1 }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)

function Find-ById($id) {
    $c = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
    $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
}
function Find-ByName($name) {
    $c = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $name)
    $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
}

$result = @{ ok = $true }

if ($Tool) {
    # 「工具」组可能是折叠的，先展开
    $grp = $null
    $c = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, "CollapsedButton")),
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, "工具")))
    $grp = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
    $id = @{ pencil = "PencilTool"; fill = "FillTool"; eraser = "EraserTool"; text = "TextTool" }[$Tool]
    if (-not $id) { Write-Output '{"ok":false,"err":"unknown tool"}'; exit 2 }
    $btn = Find-ById $id
    if (-not $btn -and $grp) {
        try { $grp.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
        Start-Sleep -Milliseconds 500
        $btn = Find-ById $id
    }
    if (-not $btn) { Write-Output ('{"ok":false,"err":"tool not found: ' + $id + '"}'); exit 3 }
    # Invoke 对这类工具栏按钮常常无效，直接按坐标点
    $r = $btn.Current.BoundingRectangle
    [PT]::Click([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2))
    Start-Sleep -Milliseconds 300
    $result.tool = $Tool
}

if ($Zoom -gt 0) {
    $sl = Find-ById "ZoomSliderControl"
    if ($sl) {
        try {
            $rv = $sl.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern)
            $rv.SetValue([double]$Zoom)
            $result.zoom = $rv.Current.Value
        } catch { $result.zoom_err = "$_" }
    } else { $result.zoom_err = "no ZoomSliderControl" }
    Start-Sleep -Milliseconds 300
}

$canvas = Find-ById "image"
if ($canvas) { $result.canvas_tool = $canvas.Current.Name }
Write-Output ($result | ConvertTo-Json -Compress)
