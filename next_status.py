"""Turn the USB debug log off (cmd 108 = 0) and read the status pack (cmd 101) the configurator shows."""
import ctypes, struct, time
from ctypes import wintypes
from protocol import take_frames, parse_answer11
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

def ask(cmd, payload=b'', wait=1.5):
    fr = sldn(cmd, payload); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    return bytes(buf)

try:
    ask(108, b'\x00', 2.0)
    for attempt in range(3):
        buf = ask(101)
        p = buf.rfind(b'SLDN\x22\x00')
        if p >= 0 and len(buf) >= p + 42:
            b = buf[p + 6:p + 40]
            ok = struct.unpack_from('<H', buf, p + 40)[0] == crc16(buf[p + 4:p + 40])
            dt, status = struct.unpack_from('<IH', b, 3)
            print(f"crc ok={ok} time={dt} status=0x{status:04x} fw={b[9]} build={struct.unpack_from('<I', b, 10)[0]}"
                  f" boot={b[14]} hw={b[15]} csq={b[16]} balance={struct.unpack_from('<H', b, 17)[0]}"
                  f" creg={b[19]} cgreg={b[20]} connected={b[21]} netType={b[22]} periph={b[23:31].hex(' ')}"
                  f" ids={struct.unpack_from('<H', b, 31)[0]} module={b[33]}")
            break
        for _, cmd, payload in take_frames(buf)[0]:
            if cmd == 11: print("answer11:", parse_answer11(payload, 101))
        print("no status pack, raw tail:", buf[-120:].hex(' '))
finally:
    k.CloseHandle(h)
