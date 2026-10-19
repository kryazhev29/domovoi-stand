"""Wi-Fi-мост DR164 по USB (команда 113): записать точку доступа или прочитать состояние.
  python wifi_setup.py                 состояние: [канал через мост 0/1, связь 0 нет точки / 1 точка есть / 2 TCP открыт]
  python wifi_setup.py SSID пароль     записать точку доступа (пустой пароль - открытая сеть); мост перезапустится
Перед 113 уходит ПИН (102). Плата должна быть в активном режиме 485 с типом 1 в таблице периферии."""
import ctypes, struct, sys, time, os
from ctypes import wintypes as W
from protocol import take_frames

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
        else:
            time.sleep(0.05)
    return got

h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == W.HANDLE(-1).value:
    raise SystemExit(f"COM5 не открылся, winerror={ctypes.get_last_error()} (закрыть NextConfig?)")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
pin = os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0')
exchange(h, sldn(102, pin), 0.3)
if len(sys.argv) >= 2:
    ssid = sys.argv[1].encode('utf-8'); key = (sys.argv[2] if len(sys.argv) > 2 else '').encode('utf-8')
    if not ssid or len(ssid) > 32 or len(key) > 63:
        raise SystemExit('SSID 1-32 байта, пароль до 63')
    payload = ssid + b'\0' + key + b'\0'
else:
    payload = b''
raw = exchange(h, sldn(113, payload), 1.0)
k.CloseHandle(h)
answered = False
frames, _rest = take_frames(raw)
for _uniq, cmd, data in frames:
    if cmd == 113:
        answered = True; data = bytes(data)
        if payload:
            print({0: 'принято: мост перезапустится, канал откроется заново через ~10 с', 1: 'занято: съёмка, слот передачи или прежняя настройка', 2: 'неверные данные'}.get(data[0] if data else 255, f'код {data}'))
        else:
            link = data[1] if len(data) > 1 else 255
            print(f"канал через мост: {'да' if data and data[0] else 'нет'}; связь: "
                  + {0: 'точки доступа нет', 1: 'точка доступа есть, TCP нет', 2: 'TCP сервера открыт'}.get(link, str(link)))
if not answered:
    print('ответа 113 нет:', raw.hex() if raw else 'пусто')
