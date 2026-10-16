"""Следить за выходами платы по USB (статус 101, бит 1 — выход 1, бит 0 — выход 2).
Печатает время и состояние при каждой смене. Переоткрывает порт после перезагрузки.
Запуск: python out_watch.py <секунд> [COM5]"""
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

dur = float(sys.argv[1]); port = sys.argv[2] if len(sys.argv) > 2 else 'COM5'
body = bytes([255, 1, 101]); lb = struct.pack('<H', len(body))
frame = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
h = None; last = 'start'; t0 = time.monotonic()

def say(text):
    print(f'{time.strftime("%H:%M:%S")} +{time.monotonic() - t0:6.1f} с  {text}', flush=True)

while time.monotonic() - t0 < dur:
    if h is None:
        h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
        if h == wintypes.HANDLE(-1).value:
            h = None
            if last != 'нет порта': say('нет порта'); last = 'нет порта'
            time.sleep(0.5); continue
        k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 500)))
    ok = k.WriteFile(h, frame, len(frame), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + 0.25
    while ok and time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        elif not n.value and len(buf) >= 42:
            break
    p = bytes(buf).rfind(b'SLDN\x22\x00')
    if not ok or p < 0 or len(buf) < p + 42:
        if not ok:
            k.CloseHandle(h); h = None
        if last != 'нет ответа': say('нет ответа'); last = 'нет ответа'
        continue
    st = struct.unpack_from('<H', buf, p + 13)[0]
    now = f'выход1={(st >> 1) & 1} выход2={st & 1}'
    if now != last: say(now); last = now
if h: k.CloseHandle(h)
say('конец')
