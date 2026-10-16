"""Wait for COM5 to disappear, come back, and answer the old bootloader ident (SLGT 0x00). Read-only."""
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

b = bytes([0xAA, 0x00])
IDENT = b'SLGT' + struct.pack('<H', len(b)) + b + struct.pack('<H', crc16(b))

def present():
    return any(c.device == 'COM5' for c in list_ports.comports())

import sys
gone = '--now' in sys.argv
deadline = time.monotonic() + (8 if gone else 20 * 60)
log("waiting: COM5 must disappear first")
while time.monotonic() < deadline:
    if not present():
        if not gone:
            log("COM5 gone"); gone = True
        time.sleep(0.2); continue
    if not gone:
        time.sleep(0.3); continue
    h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
    if h == INVALID:
        time.sleep(0.5); continue
    try:
        k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 1000)))
        n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(512)
        k.WriteFile(h, IDENT, len(IDENT), ctypes.byref(n), None)
        buf = bytearray(); end = time.monotonic() + 1.0
        while time.monotonic() < end:
            if k.ReadFile(h, tmp, 512, ctypes.byref(n), None) and n.value:
                buf += tmp.raw[:n.value]
        p = buf.find(b'SLGT')
        if p >= 0 and len(buf) >= p + 11 and buf[p + 7] == 0x00:
            d = buf[p + 8:]
            log(f"BOOTLOADER: mode={d[0]} boot=0x{d[1]:02x} hw={d[2]}"); raise SystemExit(0)
        log("COM5 present, no bootloader answer:", buf[:40].hex(' ') or '-')
    finally:
        k.CloseHandle(h)
    time.sleep(1)
log("timeout"); raise SystemExit(1)
