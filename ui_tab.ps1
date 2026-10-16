param([string]$ProcName = 'NextConfig', [int]$Index = 0)

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Tabs {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);

  public static IntPtr FindTab(uint pid) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr top, IntPtr l) {
      uint p; GetWindowThreadProcessId(top, out p);
      if (p != pid || !IsWindowVisible(top)) return true;
      EnumChildWindows(top, delegate(IntPtr h, IntPtr l2) {
        StringBuilder c = new StringBuilder(256);
        GetClassName(h, c, 256);
        if (c.ToString().Contains("SysTabControl32")) { found = h; return false; }
        return true;
      }, IntPtr.Zero);
      return found == IntPtr.Zero;
    }, IntPtr.Zero);
    return found;
  }
  public static void Select(IntPtr tab, int i) {
    SendMessage(tab, 0x1330, (IntPtr)i, IntPtr.Zero); // TCM_SETCURFOCUS
  }
}
"@

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { "no process"; exit 1 }
$tab = [Tabs]::FindTab([uint32]$p.Id)
if ($tab -eq [IntPtr]::Zero) { "no tab control"; exit 1 }
[Tabs]::Select($tab, $Index)
"tab $Index selected"
