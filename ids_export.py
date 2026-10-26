"""Выгрузка базы ID с платы страницами (команда USB 116, прошивка с 26.10.2026).
  python ids_export.py <файл.bin>   сохранить записи по 32 Б подряд и напечатать итог
Перед 116 уходит ПИН (102). Сверка: число записей = ids из next_status.py."""
import ctypes, struct, sys, time, os
from ctypes import wintypes as W
from protocol import take_frames

sys.stdout.reconfigure(encoding='utf-8')
k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = W.HANDLE

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

class T(ctypes.Structure):
    _fields_ = [(n, W.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]

def sldn(cmd, payload=b''):
    body = bytes([255, 1, cmd]) + payload; lb = struct.pack('<H', len(body))
    return b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))

def exchange(h, frame, want, wait=1.0):
    n = W.DWORD(); k.WriteFile(h, frame, len(frame), ctypes.byref(n), None)
    buf = ctypes.create_string_buffer(512); got = b''; end = time.time() + wait
    while time.time() < end:
        if k.ReadFile(h, buf, 512, ctypes.byref(n), None) and n.value:
            got += buf.raw[:n.value]
            for _u, cmd, data in take_frames(got)[0]:
                if cmd == want: return bytes(data)
        else:
            time.sleep(0.005)
    return None

if len(sys.argv) < 2: raise SystemExit(__doc__)
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == W.HANDLE(-1).value:
    raise SystemExit(f"COM5 не открылся, winerror={ctypes.get_last_error()} (закрыть NextConfig?)")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
pin = os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0')
exchange(h, sldn(102, pin), 102, 0.3)
started = time.time(); cell = 1; pages = busy = 0; records = []
try:
    while True:
        data = exchange(h, sldn(116, struct.pack('<H', cell)), 116, 1.5)
        if data is None: raise SystemExit(f'ответа 116 нет на ячейке {cell} (прошивка без 116 или плата молчит)')
        pages += 1
        if data[0]:
            busy += 1
            if busy > 50: raise SystemExit(f'флеш занята 50 раз подряд на ячейке {cell}')
            time.sleep(0.2); continue
        busy = 0
        records += [data[4 + 32 * i:36 + 32 * i] for i in range(data[3])]
        cell = struct.unpack('<H', data[1:3])[0]
        if not cell: break
finally:
    k.CloseHandle(h)
open(sys.argv[1], 'wb').write(b''.join(records))
cells = [struct.unpack('<H', r[:2])[0] for r in records]
print(f'записей {len(records)}, страниц {pages}, за {time.time() - started:.1f} с; ячейки {min(cells, default=0)}…{max(cells, default=0)}, повторов {len(cells) - len(set(cells))}')
for r in records[:3] + records[-3:]:
    ident = r[2:12].split(b'\0')[0].split(b'\xff')[0].decode('ascii', 'replace')
    print(f'  ячейка {struct.unpack("<H", r[:2])[0]:5d}  ID {ident:10s}  группа {r[12]}  тип {r[19]:02x}  в сутки {r[20]}')
