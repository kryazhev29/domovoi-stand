"""Замеры по тактам ядра (сборки prof/asm и prof/c): команда 112 по USB, за ней пакет 113.
  python prof_read.py [COM5]      показать замеры с прошлого чтения и обнулить их на плате
Время в мкс (72 МГц). Первое чтение после старта включает загрузку - его лучше выбросить."""
import ctypes, struct, sys, time
from ctypes import wintypes

NAMES = ['gsm_check_data', 'port_check_data', 'usb_check_data', 'user_loop', 'user_storage_poll',
         'весь цикл', 'проверка карты (WG_readedID)', 'карта -> реле']
MHZ = 72
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


def frames(buf):
    """Все целые кадры SLDN с верной CRC: (команда, данные)."""
    out, p = [], 0
    while (p := buf.find(b'SLDN', p)) >= 0 and p + 6 <= len(buf):
        ln = struct.unpack_from('<H', buf, p + 4)[0]
        end = p + 6 + ln + 2
        if end <= len(buf) and struct.unpack_from('<H', buf, end - 2)[0] == crc16(buf[p + 4:end - 2]):
            out.append((buf[p + 8], buf[p + 9:end - 2]))
            p = end
        else:
            p += 1
    return out


port = sys.argv[1] if len(sys.argv) > 1 else 'COM5'
h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f'порт {port} не открылся, winerror={ctypes.get_last_error()}')
try:
    k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
    body = bytes([255, 1, 112]); lb = struct.pack('<H', len(body))
    fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    n, tmp, buf = wintypes.DWORD(), ctypes.create_string_buffer(4096), bytearray()
    k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
finally:
    k.CloseHandle(h)

data = next((d for c, d in frames(bytes(buf)) if c == 113), None)
if data is None:
    raise SystemExit('пакета 113 нет - на плате прошивка без замеров?')
ver = struct.unpack_from('<I', data)[0]
print(f'замеры v{ver}, {time.strftime("%H:%M:%S")}')
print(f'{"точка":32} {"раз":>9} {"среднее мкс":>12} {"максимум мкс":>13}')
for i, name in enumerate(NAMES):
    mx, cnt, s = struct.unpack_from('<III', data, 4 + 12 * i)
    avg = s * 256 / cnt / MHZ if cnt else 0
    print(f'{name:32} {cnt:9} {avg:12.1f} {mx / MHZ:13.1f}')
