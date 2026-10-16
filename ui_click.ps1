param([string]$ProcName = 'NextConfig', [int]$WindowIndex = -1, [int]$ButtonIndex = 0, [switch]$List)

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Ui {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

  public static List<IntPtr> Tops(uint pid) {
    List<IntPtr> list = new List<IntPtr>();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) list.Add(h);
      return true;
    }, IntPtr.Zero);
    return list;
  }
  public static List<IntPtr> Buttons(IntPtr parent) {
    List<IntPtr> list = new List<IntPtr>();
    EnumChildWindows(parent, delegate(IntPtr h, IntPtr l) {
      StringBuilder c = new StringBuilder(256);
      GetClassName(h, c, 256);
      if (c.ToString().ToUpper().Contains("BUTTON") && IsWindowVisible(h)) list.Add(h);
      return true;
    }, IntPtr.Zero);
    return list;
  }
  public static string Text(IntPtr h) {
    StringBuilder s = new StringBuilder(256);
    GetWindowText(h, s, 256);
    return s.ToString();
  }
  public static void Click(IntPtr h) {
    PostMessage(h, 0x00F5, IntPtr.Zero, IntPtr.Zero);
  }
}
"@

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { "no process"; exit 1 }

$tops = [Ui]::Tops([uint32]$p.Id)
if ($tops.Count -eq 0) { "no visible window"; exit 1 }

# -1 = smallest window (a dialog sits on top of the main form)
if ($WindowIndex -lt 0) {
  $best = $null; $bestArea = [int]::MaxValue
  foreach ($h in $tops) {
    $r = New-Object Ui+RECT
    [Ui]::GetWindowRect($h, [ref]$r) | Out-Null
    $a = ($r.Right - $r.Left) * ($r.Bottom - $r.Top)
    if ($a -lt $bestArea -and $a -gt 0) { $bestArea = $a; $best = $h }
  }
} else {
  $best = $tops[$WindowIndex]
}

$buttons = [Ui]::Buttons($best)
if ($List) {
  for ($i = 0; $i -lt $buttons.Count; $i++) { "{0}: {1}" -f $i, [Ui]::Text($buttons[$i]) }
  exit 0
}
if ($buttons.Count -le $ButtonIndex) { "only $($buttons.Count) buttons"; exit 1 }
[Ui]::Click($buttons[$ButtonIndex])
"clicked: " + [Ui]::Text($buttons[$ButtonIndex])
