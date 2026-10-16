"""Enable RAM-only USB debug log (SLDN cmd 108, bits: 1 gsm AT, 2 user gsm, 4 user) and record text for N seconds."""
import ctypes, struct, sys, time
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

def sldn(cmd, payload=b''):
    body = bytes([255, 1, cmd]) + payload; lb = struct.pack('<H', len(body))
    return b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))

seconds, bits, out = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
# С 18.09.2026 команды по USB, кроме 100/101/102/111, работают только после ПИНа (команда 102).
import os as _os
_b = bytes([255, 1, 102]) + _os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'); _lb = struct.pack('<H', len(_b))
_f = b'SLDN' + _lb + _b + struct.pack('<H', crc16(_lb + _b)); _n = wintypes.DWORD()
k.WriteFile(h, _f, len(_f), ctypes.byref(_n), None); time.sleep(0.3)
k.ReadFile(h, ctypes.create_string_buffer(512), 512, ctypes.byref(_n), None)
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
data = bytearray()
try:
    fr = sldn(108, bytes([bits]))
    print("TX", fr.hex(' '), "write", bool(k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)), flush=True)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            data += tmp.raw[:n.value]
            open(out, 'wb').write(data)
finally:
    # Turn the log off and keep reading while the firmware drains its USB queue:
    # closing the port with the log on blocks the firmware until the watchdog resets it.
    fr = sldn(108, b'\x00'); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    end = time.monotonic() + 2
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            data += tmp.raw[:n.value]
    open(out, 'wb').write(data)
    k.CloseHandle(h)
print("received", len(data), "bytes")
