# Прокрутить содержимое вкладки запущенной программы (для проверки внешнего вида).
# Плату не трогает: работает только с окнами.
#   ui_scroll.ps1 -Lines 6      вниз на 6 шагов колеса
#   ui_scroll.ps1 -Lines -6     вверх
param([string]$ProcName = 'NextConfig', [int]$Lines = 3)

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Scroll {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

  // самая большая видимая панель внутри вкладки - её и крутим
  public static IntPtr Page(uint pid) {
    IntPtr best = IntPtr.Zero; int bestArea = 0;
    EnumWindows(delegate(IntPtr top, IntPtr l) {
      uint p; GetWindowThreadProcessId(top, out p);
      if (p != pid || !IsWindowVisible(top)) return true;
      EnumChildWindows(top, delegate(IntPtr h, IntPtr l2) {
        if (!IsWindowVisible(h)) return true;
        RECT r; GetWindowRect(h, out r);
        int area = (r.Right - r.Left) * (r.Bottom - r.Top);
        StringBuilder c = new StringBuilder(256); GetClassName(h, c, 256);
        if (c.ToString().Contains("WindowsForms") && area > bestArea) { bestArea = area; best = h; }
        return true;
      }, IntPtr.Zero);
      return true;
    }, IntPtr.Zero);
    return best;
  }

  public static void Wheel(IntPtr h, int lines) {
    RECT r; GetWindowRect(h, out r);
    int x = (r.Left + r.Right) / 2, y = (r.Top + r.Bottom) / 2;
    int delta = -120 * lines;
    IntPtr wp = (IntPtr)((delta << 16) & unchecked((int)0xFFFF0000));
    IntPtr lp = (IntPtr)((y << 16) | (x & 0xFFFF));
    SendMessage(h, 0x020A, wp, lp); // WM_MOUSEWHEEL
  }
}
"@

[Scroll]::SetProcessDPIAware() | Out-Null

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { 'no process'; exit 1 }

$page = [Scroll]::Page([uint32]$p.Id)
if ($page -eq [IntPtr]::Zero) { 'no page'; exit 1 }

[Scroll]::Wheel($page, $Lines)
"scrolled $Lines"
