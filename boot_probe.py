"""Read-only probe of the old bootloader: 0x00 ident and 0x15 flash CRC check."""
import ctypes, struct, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]

h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(256)
try:
    for cmd in (0x00, 0x15):
        body = bytes([0xAA, cmd]); fr = b'SLGT' + struct.pack('<H', 2) + body + struct.pack('<H', crc16(body))
        ok = k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
        buf = bytearray(); end = time.monotonic() + 3
        while ok and time.monotonic() < end:
            if k.ReadFile(h, tmp, 256, ctypes.byref(n), None) and n.value:
                buf += tmp.raw[:n.value]
            p = buf.find(b'SLGT')
            if p >= 0 and len(buf) >= p + 8 + struct.unpack_from('<H', buf, p + 4)[0]:
                break
        print(f"cmd 0x{cmd:02x}: write={bool(ok)} RX {buf.hex(' ') if buf else '(nothing)'}")
        time.sleep(0.3)
finally:
    k.CloseHandle(h)
