# Вписать текст в поле ввода запущенной программы (для проверки внешнего вида).
# Плату не трогает: работает только с окнами.
#   ui_text.ps1 -List                 список полей окна
#   ui_text.ps1 -Index 0 -Text "..."  вписать текст в поле номер 0
param([string]$ProcName = 'NextConfig', [int]$Index = 0, [string]$Text = '', [switch]$List)

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Edits {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr wp, string lp);

  public static List<IntPtr> Find(uint pid) {
    List<IntPtr> found = new List<IntPtr>();
    EnumWindows(delegate(IntPtr top, IntPtr l) {
      uint p; GetWindowThreadProcessId(top, out p);
      if (p != pid || !IsWindowVisible(top)) return true;
      EnumChildWindows(top, delegate(IntPtr h, IntPtr l2) {
        StringBuilder c = new StringBuilder(256);
        GetClassName(h, c, 256);
        if (c.ToString().ToUpper().Contains(".EDIT.") && IsWindowVisible(h)) found.Add(h);
        return true;
      }, IntPtr.Zero);
      return true;
    }, IntPtr.Zero);
    return found;
  }

  public static string TextOf(IntPtr h) {
    StringBuilder s = new StringBuilder(1024);
    GetWindowText(h, s, 1024);
    return s.ToString();
  }

  public static void SetText(IntPtr h, string t) {
    SendMessage(h, 0x000C, IntPtr.Zero, t); // WM_SETTEXT
  }
}
"@

$p = Get-Process $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { 'no process'; exit 1 }

$edits = [Edits]::Find([uint32]$p.Id)
if ($edits.Count -eq 0) { 'no edit fields'; exit 1 }

if ($List) {
  for ($i = 0; $i -lt $edits.Count; $i++) { "{0}: {1}" -f $i, [Edits]::TextOf($edits[$i]) }
  exit 0
}

if ($Index -ge $edits.Count) { "only $($edits.Count) fields"; exit 1 }

[Edits]::SetText($edits[$Index], $Text)
"field $Index set to: $Text"
