"""Выгрузить журнал событий по USB (команда 9, ответ - пакет 10) и напечатать записи.
Внимание: выгрузка до конца стирает журнал на плате, как и выгрузка сервером.
Во время обновления через сервер не запускать - сервер не получит события.
Запуск: py -3.11 next_journal.py [COM5]"""
import ctypes, struct, sys, time, datetime
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

port = sys.argv[1] if len(sys.argv) > 1 else 'COM5'
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

def ask(cmd, payload=b'', wait=1.5):
    fr = sldn(cmd, payload); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    return bytes(buf)

def pack10(buf):
    """Найти в потоке пакет 10 с верной CRC: (остаток, записи)."""
    p = 0
    while (p := buf.find(b'SLDN', p)) >= 0:
        if len(buf) >= p + 6:
            ln = struct.unpack_from('<H', buf, p + 4)[0]
            end = p + 6 + ln
            if len(buf) >= end + 2 and buf[p + 8] == 10 and \
               struct.unpack_from('<H', buf, end)[0] == crc16(buf[p + 4:end]):
                rest, cnt = struct.unpack_from('<HB', buf, p + 9)
                return rest, [buf[p + 12 + i * 32:p + 44 + i * 32] for i in range(cnt)]
        p += 4
    return None

try:
    ask(108, b'\x00', 2.0)  # отладочный вывод выключить, чтобы не мешал разбору
    total = 0
    for _ in range(200):
        r = pack10(ask(9))
        if r is None:
            print('нет ответа 10'); break
        rest, recs = r
        for c in recs:
            idx, dt, typ = struct.unpack_from('<IIB', c)
            ident = c[9:19].split(b'\0')[0].decode('cp1251', 'replace')
            photo, passes = struct.unpack_from('<IB', c, 19)
            when = datetime.datetime.fromtimestamp(dt, datetime.timezone.utc).strftime('%d.%m %H:%M:%S')
            print(f"#{idx:<6} {when} UTC  событие {typ:<3} ID «{ident}»  фото {photo}  проходы {passes}")
        total += len(recs)
        if not recs:
            break
    print(f'записей: {total}')
finally:
    k.CloseHandle(h)
