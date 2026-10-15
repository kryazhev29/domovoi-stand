"""Отправить SMS с платы по USB командой 11 (как сервер). Событие 36 в журнале.
  python usb_sms.py <10 цифр номера> [текст до 50 латинских символов] [COM5]
ПИН для USB-команд — DOMOVOI_PIN (по умолчанию 4321). NextConfig закрыть."""
import ctypes, os, struct, sys, time
from ctypes import wintypes as W
from protocol import take_frames, parse_answer11

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


phone = sys.argv[1][-10:]
text = sys.argv[2] if len(sys.argv) > 2 else 'DOMOVOI USB test'
port = sys.argv[3] if len(sys.argv) > 3 else 'COM5'
h = W.HANDLE(k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None))
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
n, tmp = W.DWORD(), ctypes.create_string_buffer(4096)


def ask(cmd, payload, uniq, wait=3.0):
    body = bytes([255, uniq, cmd]) + payload; lb = struct.pack('<H', len(body))
    fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf, end = bytearray(), time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    return bytes(buf)


try:
    pin = os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0')
    print('ПИН:', ask(102, pin, 1, 1.0).hex(' '))
    r = ask(11, phone.encode('ascii') + text.encode('ascii')[:50].ljust(50, b'\0'), 2, 40.0)
    print('ответ на 11:', r.hex(' '))
    for uid, cmd, payload in take_frames(r)[0]:
        if uid == 2 and cmd == 11:
            print('код', parse_answer11(payload, 11)[0], '(0 = выполнено)')
finally:
    k.CloseHandle(h)
