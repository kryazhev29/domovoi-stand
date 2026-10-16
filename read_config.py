"""Read-only: settings of the board over USB (SLDN command 106), print main fields.
  python read_config.py [file.bin]    optional copy of the 512 raw bytes
Nothing is written to the board. Close NextConfig first."""
import ctypes, struct, sys, time
from ctypes import wintypes as W

FIELDS = [('datetime', 0, 4, 'u'), ('server1', 8, 32, 's'), ('server2', 40, 32, 's'), ('NTPserver', 72, 32, 's'),
          ('netOperator', 104, 32, 's'), ('netType', 136, 1, 'u'), ('countryCode', 137, 8, 's'),
          ('useServer1', 219, 1, 'u'), ('useServer2', 220, 1, 'u')]

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

class T(ctypes.Structure):
    _fields_ = [(n, W.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]

k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.restype = W.HANDLE
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == W.HANDLE(-1).value:
    raise SystemExit(f'open failed, winerror={ctypes.get_last_error()}')
try:
    k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
    # С 18.09.2026 команды по USB, кроме 100/101/102/111, работают только после ПИНа (команда 102).
    import os as _os
    _b = bytes([255, 1, 102]) + _os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'); _lb = struct.pack('<H', len(_b))
    _f = b'SLDN' + _lb + _b + struct.pack('<H', crc16(_lb + _b)); _n = W.DWORD()
    k.WriteFile(h, _f, len(_f), ctypes.byref(_n), None); time.sleep(0.3)
    k.ReadFile(h, ctypes.create_string_buffer(512), 512, ctypes.byref(_n), None)
    body = bytes([255, 1, 106]); lb = struct.pack('<H', len(body))
    frame = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    n = W.DWORD()
    k.WriteFile(h, frame, len(frame), ctypes.byref(n), None)
    buf, tmp, raw = bytearray(), ctypes.create_string_buffer(4096), None
    end = time.monotonic() + 6
    while raw is None and time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        p = buf.find(b'SLDN')
        while p >= 0 and len(buf) >= p + 8:
            ln = struct.unpack_from('<H', buf, p + 4)[0]
            if len(buf) < p + 8 + ln:
                break
            if buf[p + 8] == 106 and struct.unpack_from('<H', buf, p + 6 + ln)[0] == crc16(buf[p + 4:p + 6 + ln]):
                raw = bytes(buf[p + 9:p + 6 + ln]); break
            p = buf.find(b'SLDN', p + 1)
finally:
    k.CloseHandle(h)
if raw is None:
    raise SystemExit('no answer to 106')
ok = len(raw) == 512 and crc16(raw[:510]) == struct.unpack_from('<H', raw, 510)[0]
print(time.strftime('%H:%M:%S'), 'len', len(raw), 'crc ok' if ok else 'CRC BAD')
if len(sys.argv) > 1:
    open(sys.argv[1], 'wb').write(raw)
for name, off, size, kind in FIELDS:
    b = raw[off:off + size]
    print(f'{name:12} ' + (repr(b.split(b'\0')[0].decode('ascii', 'replace')) if kind == 's' else str(int.from_bytes(b, 'little'))))
