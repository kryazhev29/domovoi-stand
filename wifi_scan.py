"""Список сетей Wi-Fi, которые видит мост DR164 (команда USB 115, прошивка с 22.10.2026).
  python wifi_scan.py            искать заново и напечатать список (уровень %, имя), сильные первыми
  python wifi_scan.py --no-reset продолжить чтение готового списка без нового поиска
Перед 115 уходит ПИН (102). Плата должна быть с выбранным Wi-Fi-модемом (NextConfig → «Связь»)."""
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

def exchange(h, frame, wait=1.0):
    n = W.DWORD(); k.WriteFile(h, frame, len(frame), ctypes.byref(n), None)
    buf = ctypes.create_string_buffer(512); got = b''; end = time.time() + wait
    while time.time() < end:
        if k.ReadFile(h, buf, 512, ctypes.byref(n), None) and n.value:
            got += buf.raw[:n.value]
            frames, _ = take_frames(got)
            if any(cmd == 115 for _u, cmd, _d in frames): break
        else:
            time.sleep(0.02)
    return got

def ask(h, row):
    raw = exchange(h, sldn(115, bytes([row])), 1.5)
    for _uniq, cmd, data in take_frames(raw)[0]:
        if cmd == 115: return bytes(data)
    return None

STATUS = {1: 'поиск идёт', 2: 'Wi-Fi-модем не выбран', 3: 'мост не ответил'}
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == W.HANDLE(-1).value:
    raise SystemExit(f"COM5 не открылся, winerror={ctypes.get_last_error()} (закрыть NextConfig?)")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
pin = os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0')
exchange(h, sldn(102, pin), 0.3)
started = time.time(); row = 0 if '--no-reset' in sys.argv else 255; networks = []
try:
    while True:
        data = ask(h, row)
        if data is None:
            raise SystemExit('ответа 115 нет (прошивка без команды 115 или плата молчит)')
        if data[0] == 1:
            if time.time() - started > 20: raise SystemExit('поиск не закончился за 20 с')
            print('.', end='', flush=True); time.sleep(0.4); row = 0; continue
        if data[0] != 0:
            raise SystemExit(STATUS.get(data[0], f'код {data[0]}'))
        at = 2
        while at + 2 <= len(data):
            n = data[at + 1]; networks.append((data[at], data[at + 2:at + 2 + n].decode('utf-8', 'replace'))); at += 2 + n
        if data[1] == 255: break
        row = data[1]
finally:
    k.CloseHandle(h)
print(f'\nнайдено сетей: {len(networks)} за {time.time() - started:.1f} с')
for level, name in networks:
    print(f'{level:4d} %  {name}')
