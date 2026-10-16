# Выбрать значение в выпадающем списке запущенной программы (для проверки вида).
# Плату не трогает: работает только с окном.
#   ui_combo.ps1 -List                      списки окна и что в них выбрано
#   ui_combo.ps1 -Index 0 -Value "Авто"     выбрать значение в списке номер 0
param([string]$ProcName = 'NextConfig', [int]$Index = 0, [string]$Value = '', [switch]$List)

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Combos {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr SendMessageText(IntPtr h, uint msg, IntPtr wp, StringBuilder lp);

  const uint CB_GETCURSEL = 0x0147, CB_SETCURSEL = 0x014E, CB_GETCOUNT = 0x0146, CB_GETLBTEXT = 0x0148;

  public static List<IntPtr> Find(uint pid) {
    List<IntPtr> found = new List<IntPtr>();
    EnumWindows(delegate(IntPtr top, IntPtr l) {
      uint p; GetWindowThreadProcessId(top, out p);
      if (p != pid || !IsWindowVisible(top)) return true;
      EnumChildWindows(top, delegate(IntPtr h, IntPtr l2) {
        StringBuilder c = new StringBuilder(256);
        GetClassName(h, c, 256);
        if (c.ToString().ToUpper().Contains(".COMBOBOX") && IsWindowVisible(h)) found.Add(h);
        return true;
      }, IntPtr.Zero);
      return true;
    }, IntPtr.Zero);
    return found;
  }

  public static int Count(IntPtr h) { return (int)SendMessage(h, CB_GETCOUNT, IntPtr.Zero, IntPtr.Zero); }
  public static int Current(IntPtr h) { return (int)SendMessage(h, CB_GETCURSEL, IntPtr.Zero, IntPtr.Zero); }

  public static string ItemText(IntPtr h, int i) {
    StringBuilder s = new StringBuilder(512);
    SendMessageText(h, CB_GETLBTEXT, (IntPtr)i, s);
    return s.ToString();
  }

  // CB_SETCURSEL сам по себе меняет только сам список: программа о выборе не
  // узнаёт. Поэтому родителю отправляем извещение, как это делает Windows.
  public static void Select(IntPtr h, int i) {
    SendMessage(h, CB_SETCURSEL, (IntPtr)i, IntPtr.Zero);
    IntPtr parent = GetParent(h);
    if (parent != IntPtr.Zero) {
      const int CBN_SELCHANGE = 1;
      IntPtr wp = (IntPtr)(CBN_SELCHANGE << 16);
      SendMessage(parent, 0x0111, wp, h); // WM_COMMAND
    }
  }
}
"@

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { 'no process'; exit 1 }

$combos = [Combos]::Find([uint32]$p.Id)
if ($combos.Count -eq 0) { 'no combo boxes'; exit 1 }

if ($List) {
  for ($i = 0; $i -lt $combos.Count; $i++) {
    $h = $combos[$i]
    $cur = [Combos]::Current($h)
    $txt = if ($cur -ge 0) { [Combos]::ItemText($h, $cur) } else { '(не выбрано)' }
    "{0}: {1}" -f $i, $txt
  }
  exit 0
}

if ($Index -ge $combos.Count) { "only $($combos.Count) combo boxes"; exit 1 }

$h = $combos[$Index]
$n = [Combos]::Count($h)

for ($i = 0; $i -lt $n; $i++) {
  if ([Combos]::ItemText($h, $i) -eq $Value) {
    [Combos]::Select($h, $i)
    "combo $Index set to: $Value"
    exit 0
  }
}

"no item '$Value'"
exit 1
