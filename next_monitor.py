"""Poll status pack (cmd 101) every 5 s for N seconds, reopening the port each time; log uptime counter, GSM flags, USB presence."""
import ctypes, struct, sys, time
from ctypes import wintypes
import serial.tools.list_ports as list_ports
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

body = bytes([255, 1, 101]); lb = struct.pack('<H', len(body))
REQ = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)

end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    if not any(c.device == 'COM5' for c in list_ports.comports()):
        log("COM5 absent"); time.sleep(1); continue
    h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
    if h == wintypes.HANDLE(-1).value:
        log("open failed", ctypes.get_last_error()); time.sleep(2); continue
    try:
        k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 1000)))
        n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
        ok = k.WriteFile(h, REQ, len(REQ), ctypes.byref(n), None)
        buf = bytearray(); t = time.monotonic() + 1.2
        while ok and time.monotonic() < t:
            if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
                buf += tmp.raw[:n.value]
        p = buf.rfind(b'SLDN\x22\x00')
        if p >= 0 and len(buf) >= p + 42:
            b = bytes(buf[p + 6:p + 40])
            log(f"uptime={struct.unpack_from('<I', b, 3)[0]} status=0x{struct.unpack_from('<H', b, 7)[0]:04x} csq={b[16]} creg={b[19]} cgreg={b[20]} connected={b[21]} net={b[22]}")
        else:
            log(f"no status (write ok={bool(ok)}, rx={len(buf)})")
    finally:
        k.CloseHandle(h)
    time.sleep(5)
log("end")
