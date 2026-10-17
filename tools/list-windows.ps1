#requires -Version 5
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinEnum {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern int GetClassNameW(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
$rows = New-Object System.Collections.ArrayList
$fg = [WinEnum]::GetForegroundWindow()
$cb = [WinEnum+EnumProc]{
  param($h, $l)
  if (-not [WinEnum]::IsWindowVisible($h)) { return $true }
  $sb = New-Object System.Text.StringBuilder 512
  [void][WinEnum]::GetWindowTextW($h, $sb, 512)
  $title = $sb.ToString()
  $cb2 = New-Object System.Text.StringBuilder 256
  [void][WinEnum]::GetClassNameW($h, $cb2, 256)
  $cls = $cb2.ToString()
  $r = New-Object WinEnum+RECT
  [void][WinEnum]::GetWindowRect($h, [ref]$r)
  $w = $r.Right - $r.Left; $hh = $r.Bottom - $r.Top
  if ($w -le 50 -or $hh -le 50) { return $true }
  $procId = 0
  [void][WinEnum]::GetWindowThreadProcessId($h, [ref]$procId)
  $pname = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
  $enc = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($title))
  [void]$rows.Add([pscustomobject]@{hwnd=$h;pid=$procId;proc=$pname;class=$cls;rect="$($r.Left),$($r.Top),$($r.Right),$($r.Bottom)";size="${w}x${hh}";fg=($h -eq $fg);title_b64=$enc})
  return $true
}
[void][WinEnum]::EnumWindows($cb, [IntPtr]::Zero)
# decode titles to UTF-8 file to avoid console mojibake
$out = $rows | ForEach-Object {
  $t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_.title_b64))
  "proc=$($_.proc) pid=$($_.pid) hwnd=$($_.hwnd) fg=$($_.fg) rect=$($_.rect) size=$($_.size) class=$($_.class) title=$t"
}
$out | Out-File -FilePath "$env:TEMP\windows.txt" -Encoding utf8
Get-Content "$env:TEMP\windows.txt" -Encoding utf8
