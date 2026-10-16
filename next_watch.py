"""After a power cycle, probe the NEXT firmware on COM5 with read-only SLDN cmd 100 (identification)."""
import ctypes, struct, time
from ctypes import wintypes
import serial.tools.list_ports as list_ports

k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE
INVALID = wintypes.HANDLE(-1).value

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]

body = bytes([255, 1, 100]); lb = struct.pack('<H', len(body))
SLDN = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))

def present():
    return any(c.device == 'COM5' for c in list_ports.comports())

deadline = time.monotonic() + 20 * 60
gone = False
log("waiting for power cycle")
while time.monotonic() < deadline:
    if not present():
        if not gone:
            log("COM5 gone"); gone = True
        time.sleep(0.2); continue
    if not gone:
        time.sleep(0.3); continue
    log("COM5 back, probing")
    t_back = time.monotonic()
    while time.monotonic() - t_back < 90:
        if not present():
            log("COM5 gone again after %.1f s" % (time.monotonic() - t_back)); break
        h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
        if h == INVALID:
            log("open failed winerror", ctypes.get_last_error()); time.sleep(2); continue
        try:
            k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 1000)))
            n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(1024)
            ok = k.WriteFile(h, SLDN, len(SLDN), ctypes.byref(n), None); err = ctypes.get_last_error()
            buf = bytearray(); end = time.monotonic() + 1.5
            while ok and time.monotonic() < end:
                if k.ReadFile(h, tmp, 1024, ctypes.byref(n), None) and n.value:
                    buf += tmp.raw[:n.value]
            log(f"+{time.monotonic() - t_back:.0f}s write ok={bool(ok)} err={0 if ok else err} rx={len(buf)}")
            if buf:
                log("  hex:", buf.hex(' '))
                log("  ascii:", ''.join(chr(c) if 32 <= c < 127 else '.' for c in buf))
                log("DONE: firmware answered"); raise SystemExit(0)
        finally:
            k.CloseHandle(h)
        time.sleep(2)
    else:
        log("no answer for 90 s after power-up"); raise SystemExit(1)
    gone = not present()
log("timeout")
