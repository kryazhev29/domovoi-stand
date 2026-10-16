"""Прочитать по USB диагностику связи (команда 112 v3/v4/v5): код, этапы, сота, сбои."""
import ctypes, struct, time, sys, re
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
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
STAGES = ['питание', 'настройка', 'SIM', 'сеть', 'PDP', 'TCP', 'сервер', '?']
try:
    body = bytes([0, 1, 112]); lb = struct.pack('<H', len(body))
    fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + 1.5
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    match = re.search(b'SLDN[\x55\x58\x5a]\x00', buf)
    if not match:
        raise SystemExit("нет ответа 112: " + bytes(buf[-60:]).hex(' '))
    p = match.start()
    size = struct.unpack_from('<H', buf, p + 4)[0] + 8
    f = bytes(buf[p:p + size])
    if len(f) != size or f[8] != 112 or (f[9], size) not in ((3, 93), (4, 96), (5, 98)):
        raise SystemExit("неполный или неизвестный формат 112")
    ok = struct.unpack_from('<H', f, size - 2)[0] == crc16(f[4:-2])
    if not ok:
        raise SystemExit("неверная CRC ответа 112")
    plmn = f[15:22].split(b'\0')[0].decode('ascii', 'replace')
    wait, *st = struct.unpack_from('<7I', f, 22)
    names = ['питание', 'настройка', 'SIM', 'сеть', 'PDP', 'TCP']
    print(f"crc={ok} код={f[10]} csq={f[12]} сеть={f[13]} ya.ru={f[14]} plmn={plmn} ожидание={wait/1000:.1f} с")
    if f[9] >= 4:
        rsrp = 'нет данных' if f[91] == 255 else f'−{f[91]} дБм'
        rsrq = 'нет данных' if f[92] == 255 else f'−{f[92]} дБ'
        sinr = 'нет данных' if f[93] == 255 else str(f[93])
        print(f"RSRP {rsrp}, RSRQ {rsrq}, SINR сырой {sinr}")
    if f[9] >= 5:
        band = 'диапазон неизвестен' if f[94] == 255 else f'B{f[94]}'
        width = ('1,4', '3', '5', '10', '15', '20')[f[95]] if f[95] <= 5 else 'неизвестно'
        print(f"{band}, {width} МГц")
    print('этапы: ' + ', '.join(f"{a} {b/1000:.2f}" for a, b in zip(names, st)) + f" | сумма {sum(st)/1000:.2f} с")
    cnt = f[50]
    for i in range(cnt):
        w = struct.unpack_from('<I', f, 51 + 4 * i)[0]
        print(f"  сбой: этап {STAGES[w & 7]}, причина {(w >> 3) & 31}, {w >> 8} с")
finally:
    k.CloseHandle(h)
