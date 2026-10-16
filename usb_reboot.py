"""Перезагрузить плату по USB командой 2 (как сервер; в журнал - событие 33).
Запуск: py -3.11 usb_reboot.py [COM5]. NextConfig закрыть."""
import ctypes, os, struct, sys, time
from ctypes import wintypes as W

k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.restype = W.HANDLE


def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def sldn(cmd, payload):
    body = bytes([255, 1, cmd]) + payload
    lb = struct.pack('<H', len(body))
    return b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))


class T(ctypes.Structure):
    _fields_ = [(n, W.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]


port = sys.argv[1] if len(sys.argv) > 1 else 'COM5'
h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == W.HANDLE(-1).value:
    raise SystemExit(f'порт {port} не открылся, winerror={ctypes.get_last_error()}')
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
n, buf = W.DWORD(), ctypes.create_string_buffer(512)
try:
    for f in (sldn(102, os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0')), sldn(2, bytes([0]))):
        k.WriteFile(h, f, len(f), ctypes.byref(n), None)
        time.sleep(0.5)
        k.ReadFile(h, buf, 512, ctypes.byref(n), None)
        p = buf.raw[:n.value]
        i = p.find(b'SLDN')
        print('ответ:', p[i:i + 20].hex(' ') if i >= 0 else 'нет')
finally:
    k.CloseHandle(h)
