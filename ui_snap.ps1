param([string]$ProcName = 'NextConfig', [string]$Out = "$env:TEMP\shot.png", [switch]$Smallest)

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Shot {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
  // без этого PowerShell считает координаты в масштабе 100 % и снимок выходит обрезанным
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

  public static List<IntPtr> Windows(uint pid) {
    List<IntPtr> list = new List<IntPtr>();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) list.Add(h);
      return true;
    }, IntPtr.Zero);
    return list;
  }
}
"@

[Shot]::SetProcessDPIAware() | Out-Null

Add-Type -AssemblyName System.Drawing

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { "no process"; exit 1 }

$best = $null
$bestArea = $(if ($Smallest) { [int]::MaxValue } else { 0 })
foreach ($h in [Shot]::Windows([uint32]$p.Id)) {
  $r = New-Object Shot+RECT
  [Shot]::GetWindowRect($h, [ref]$r) | Out-Null
  $area = ($r.Right - $r.Left) * ($r.Bottom - $r.Top)
  if ($area -lt 10000) { continue }
  if (($Smallest -and $area -lt $bestArea) -or (-not $Smallest -and $area -gt $bestArea)) {
    $bestArea = $area; $best = $h; $bestRect = $r
  }
}
if (-not $best) { "no visible window"; exit 1 }

$w = $bestRect.Right - $bestRect.Left
$ht = $bestRect.Bottom - $bestRect.Top
$bmp = New-Object System.Drawing.Bitmap $w, $ht
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[Shot]::PrintWindow($best, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"{0} {1}x{2}" -f $Out, $w, $ht
