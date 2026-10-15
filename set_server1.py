"""Write the board settings back over USB with command 12, changing server 1 only.
Usage: py -3.11 set_server1.py <config.bin> <host:port> [COM5]
       py -3.11 set_server1.py <config.bin> --as-is [COM5]   restore the file untouched
The 512 bytes come from a copy made by read_config.py; nothing else is touched.
Close NextConfig first. Keep the board on 12 V: command 12 rewrites the sector that
holds both the settings and the device block."""
import ctypes, os, pathlib, struct, sys, time
from ctypes import wintypes as W
from protocol import parse_answer11

k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.restype = W.HANDLE
SERVER1, SIZE = 8, 32


def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


class T(ctypes.Structure):
    _fields_ = [(n, W.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]


def field(buf, off, size):
    raw = bytes(buf[off:off + size])
    return raw.split(b'\0')[0].decode('ascii', 'replace')


path = pathlib.Path(sys.argv[1])
want = sys.argv[2]
port = sys.argv[3] if len(sys.argv) > 3 else 'COM5'
cfg = bytearray(path.read_bytes())
if len(cfg) != 512:
    raise SystemExit(f'ожидалось 512 байт конфигурации, в файле {len(cfg)}')

print(f'файл      {path.name}')
print(f'было      сервер 1 = {field(cfg, SERVER1, SIZE)!r}')
if want != '--as-is':
    new = want.encode('ascii')
    if len(new) >= SIZE:
        raise SystemExit(f'адрес длиннее {SIZE - 1} символов')
    cfg[SERVER1:SERVER1 + SIZE] = new.ljust(SIZE, b'\0')
print(f'станет    сервер 1 = {field(cfg, SERVER1, SIZE)!r}')
if cfg[SERVER1] == 0 or cfg[137] == 0:
    raise SystemExit('контроллер отвергнет: сервер 1 или код страны пустые')

h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == W.HANDLE(-1).value:
    raise SystemExit(f'порт {port} не открылся, winerror={ctypes.get_last_error()}')
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
n, tmp, uniq = W.DWORD(), ctypes.create_string_buffer(4096), 0


def ask(cmd, payload, want_cmd, wait):
    global uniq
    uniq = (uniq + 1) & 255
    body = bytes([255, uniq, cmd]) + payload
    lb = struct.pack('<H', len(body))
    fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf, end = bytearray(), time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        p = 0
        while (p := buf.find(b'SLDN', p)) >= 0 and len(buf) >= p + 6:
            ln = struct.unpack_from('<H', buf, p + 4)[0]
            if len(buf) < p + 8 + ln:
                break
            if struct.unpack_from('<H', buf, p + 6 + ln)[0] == crc16(buf[p + 4:p + 6 + ln]):
                if buf[p + 8] in want_cmd:
                    return buf[p + 8], bytes(buf[p + 9:p + 6 + ln])
            p += 4
    return None, None


try:
    # С 18.09.2026 команды по USB, кроме 100/101/102/111, работают только после ПИНа.
    ask(102, os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'), (11,), 2.0)
    cmd, ans = ask(12, bytes(cfg), (11, 12), 8.0)
    if cmd == 12:
        stamp, crc = struct.unpack('<IH', ans[1:7])
        print(f'записано  таймштамп {stamp}, CRC сектора настроек {crc:#06x}')
    elif cmd == 11:
        raise SystemExit(f'контроллер отказал: код {parse_answer11(ans, 12)[0]}')
    else:
        raise SystemExit('ответа нет — настройки могли не записаться, проверьте read_config.py')
finally:
    k.CloseHandle(h)
