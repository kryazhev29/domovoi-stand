param([string]$ProcName = 'NextConfig', [string]$Path = '')

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Dlg {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr wp, string lp);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h, int id);

  public static IntPtr FindDialog(uint pid) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr top, IntPtr l) {
      uint p; GetWindowThreadProcessId(top, out p);
      if (p != pid || !IsWindowVisible(top)) return true;
      StringBuilder c = new StringBuilder(256);
      GetClassName(top, c, 256);
      if (c.ToString() == "#32770") { found = top; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public static IntPtr FindEdit(IntPtr dlg) {
    IntPtr found = IntPtr.Zero;
    EnumChildWindows(dlg, delegate(IntPtr h, IntPtr l) {
      StringBuilder c = new StringBuilder(256);
      GetClassName(h, c, 256);
      if (c.ToString() == "Edit" && IsWindowVisible(h)) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { "no process"; exit 1 }

$dlg = [Dlg]::FindDialog([uint32]$p.Id)
if ($dlg -eq [IntPtr]::Zero) { "no file dialog"; exit 1 }

$edit = [Dlg]::FindEdit($dlg)
if ($edit -eq [IntPtr]::Zero) { "no edit box"; exit 1 }

[Dlg]::SendMessage($edit, 0x000C, [IntPtr]::Zero, $Path) | Out-Null   # WM_SETTEXT
Start-Sleep -Milliseconds 300
$ok = [Dlg]::GetDlgItem($dlg, 1)                                       # IDOK
[Dlg]::PostMessage($ok, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
"path sent"
