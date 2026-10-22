"""Команда 7 по USB: снять фото камерой с адресом из таблицы 485 и показать ответ платы.
Запуск: py -3.11 usb_photo.py [адрес камеры, по умолчанию 2] [COM5]
Ответ 7: 0 - съёмка начата, 1 - нет такой камеры/режим 485 не активный, 2 - занято. Дальше смотреть usb_watch.py:
готовое фото - бит фото в статусе и событие 129. Закрыть NextConfig перед запуском."""
import ctypes, os, struct, sys, time
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


addr = int(sys.argv[1]) if len(sys.argv) > 1 else 2
port = sys.argv[2] if len(sys.argv) > 2 else 'COM5'
h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"порт {port} не открылся, winerror={ctypes.get_last_error()} (закрыта ли NextConfig?)")
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
n = wintypes.DWORD(); buf = ctypes.create_string_buffer(4096)


def ask(cmd, payload=b'', wait=0.5):
    fr = sldn(cmd, payload); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    time.sleep(wait); k.ReadFile(h, buf, 4096, ctypes.byref(n), None)
    return buf.raw[:n.value]


r = ask(102, os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'), 0.3)
print('ПИН:', r.hex(' '))
r = ask(7, bytes([addr]))
end = time.time() + 3
while time.time() < end:  # ответ может прийти не первым кадром
    k.ReadFile(h, buf, 4096, ctypes.byref(n), None); r += buf.raw[:n.value]
code = None
i = r.find(b'SLDN')
while i >= 0 and i + 9 < len(r):
    if r[i + 8] == 7 and r[i + 6] == 255:
        code = r[i + 9]
        break
    i = r.find(b'SLDN', i + 1)
print('ответ 7: код', code, {0: 'съёмка начата', 1: 'нет такой камеры', 2: 'занято'}.get(code, '?'), '| всё:', r.hex(' '))
k.CloseHandle(h)
