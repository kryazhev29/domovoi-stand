"""Команда 8 по USB и сразу статус: какие выходы включились.
Запуск: py -3.11 next_relay.py <выход1 0/1> <выход2 0/1> [источник, по умолчанию 1 - приложение] [COM5]
Источник 3 (тест из админки) включает выходы напрямую и держит до команды с 0."""
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

o1, o2 = int(sys.argv[1]), int(sys.argv[2])
args = [a for a in sys.argv[1:] if a != '--debug']
src = int(args[2]) if len(args) > 2 else 1
port = args[3] if len(args) > 3 else 'COM5'
h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"порт {port} не открылся, winerror={ctypes.get_last_error()} (закрыта ли NextConfig?)")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
# С 18.09.2026 команды по USB, кроме 100/101/102/111, работают только после ПИНа (команда 102).
import os as _os
_b = bytes([255, 1, 102]) + _os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'); _lb = struct.pack('<H', len(_b))
_f = b'SLDN' + _lb + _b + struct.pack('<H', crc16(_lb + _b)); _n = wintypes.DWORD()
k.WriteFile(h, _f, len(_f), ctypes.byref(_n), None); time.sleep(0.3)
k.ReadFile(h, ctypes.create_string_buffer(512), 512, ctypes.byref(_n), None)
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)

def ask(cmd, payload=b'', wait=0.4):
    fr = sldn(cmd, payload); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    return bytes(buf)

def outs():
    buf = ask(101)
    p = buf.rfind(b'SLDN\x22\x00')
    if p < 0 or len(buf) < p + 42:
        return None
    st = struct.unpack_from('<H', buf, p + 6 + 3 + 4)[0]
    return (st >> 1) & 1, st & 1

try:
    dbg = ask(108, b'\x04' if '--debug' in sys.argv else b'\x00', 1.5)
    print('до команды: выход1, выход2 =', outs())
    a = ask(8, bytes([src, o1, o2]), 1.5)
    print('ответ на 8:', a[-12:].hex(' '))
    if '--debug' in sys.argv: print(a.decode('cp1251', 'replace'))
    t0 = time.monotonic()
    for _ in range(8):
        print(f'+{time.monotonic() - t0:4.1f} с: выход1, выход2 =', outs())
finally:
    k.CloseHandle(h)
