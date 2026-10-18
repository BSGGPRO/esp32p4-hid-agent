# tools/paint-color.ps1 - 用画图自带调色板设置当前前景色
# usage: pwsh -NoProfile -File tools\paint-color.ps1 -Row 1 -Col 0   (Row 1/2, Col 0..8)
# 说明：画图的「编辑颜色」自定义 RGB 是 WinUI 模态窗，UIA 一碰就卡死，所以只点调色板格子。
param(
    [int]$Row = 1,
    [int]$Col = 0
)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PC {
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

function Find-Swatch1 {
    try {
        foreach ($e in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                [System.Windows.Automation.Condition]::TrueCondition)) {
            if ($e.Current.ControlType -eq [System.Windows.Automation.ControlType]::RadioButton -and
                $e.Current.Name -like '颜色 1:*' -and -not $e.Current.IsOffscreen) { return $e }
        }
    } catch {}
    return $null
}
function Expand-Colors {
    $c = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, "CollapsedButton")),
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, "颜色")))
    $g = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
    if ($g) { try { $g.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {} }
    Start-Sleep -Milliseconds 700
}

$s1 = Find-Swatch1
if (-not $s1) { Expand-Colors; $s1 = Find-Swatch1 }
if (-not $s1) { Write-Output '{"ok":false,"err":"no 颜色1 swatch"}'; exit 2 }
$r = $s1.Current.BoundingRectangle
$sx = [int]$r.X
$sy = [int]$r.Y
$before = ($s1.Current.Name -replace '^颜色 1:\s*', '')

$cx = $sx + 97 + 30 * $Col
$cy = $sy + 15 + 30 * ($Row - 1)
[PC]::Click($cx, $cy)
Start-Sleep -Milliseconds 500

$s1b = Find-Swatch1
$after = if ($s1b) { ($s1b.Current.Name -replace '^颜色 1:\s*', '') } else { "" }
Write-Output (@{ ok = $true; row = $Row; col = $Col; click = @($cx, $cy); before = $before;
        after = $after; changed = ($before -ne $after) } | ConvertTo-Json -Compress)
