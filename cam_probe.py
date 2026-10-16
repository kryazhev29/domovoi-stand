"""Проверка камеры на адаптере USB-UART (CH340 и т.п.) без платы: на каждой скорости
синхронизация C328 (AA 0D) до 60 раз и команды VC0706 (56 00 11 / 56 00 26), печать ответа.
Запуск: python cam_probe.py [COMx]   (без порта - все COM, кроме COM5 платы)."""
import ctypes, sys, time, winreg
from ctypes import wintypes as W

k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.restype = W.HANDLE


class DCB(ctypes.Structure):
    _fields_ = [('DCBlength', W.DWORD), ('BaudRate', W.DWORD), ('flags', W.DWORD), ('wReserved', W.WORD),
                ('XonLim', W.WORD), ('XoffLim', W.WORD), ('ByteSize', ctypes.c_ubyte), ('Parity', ctypes.c_ubyte),
                ('StopBits', ctypes.c_ubyte), ('XonChar', ctypes.c_char), ('XoffChar', ctypes.c_char),
                ('ErrorChar', ctypes.c_char), ('EofChar', ctypes.c_char), ('EvtChar', ctypes.c_char),
                ('wReserved1', W.WORD)]


class T(ctypes.Structure):
    _fields_ = [(n, W.DWORD) for n in 'abcde']


def ports():
    out = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DEVICEMAP\SERIALCOMM')
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            out.append((value, name)); i += 1
    except OSError:
        pass
    return out


def open_port(name, baud):
    h = k.CreateFileW('\\\\.\\' + name, 0xC0000000, 0, None, 3, 0, None)
    if h == W.HANDLE(-1).value:
        return None
    d = DCB(); d.DCBlength = ctypes.sizeof(DCB)
    k.GetCommState(h, ctypes.byref(d))
    d.BaudRate, d.ByteSize, d.Parity, d.StopBits = baud, 8, 0, 0
    d.flags = 0x1  # fBinary, без управления потоком, DTR/RTS выключены
    k.SetCommState(h, ctypes.byref(d))
    k.SetCommTimeouts(h, ctypes.byref(T(0xFFFFFFFF, 0, 0, 0, 0)))  # чтение без ожидания
    k.PurgeComm(h, 0xF)
    return h


def xfer(h, data, wait):
    n = W.DWORD(); buf = ctypes.create_string_buffer(4096); got = bytearray()
    if data:
        k.WriteFile(h, data, len(data), ctypes.byref(n), None)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, buf, 4096, ctypes.byref(n), None) and n.value:
            got += buf.raw[:n.value]
        time.sleep(0.01)
    return bytes(got)


def show(tag, data):
    if data:
        text = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[:64])
        print(f'  {tag}: {len(data)} bytes  {data[:32].hex(" ")}  |{text}|', flush=True)


targets = [sys.argv[1]] if len(sys.argv) > 1 else [p for p, _ in ports() if p.upper() != 'COM5']
print('ports:', ports(), flush=True)
for port in targets:
    print(f'=== {port}', flush=True)
    for baud in (115200, 57600, 38400, 19200, 14400, 9600):
        h = open_port(port, baud)
        if h is None:
            print(f'  cannot open {port}: {ctypes.get_last_error()}'); break
        print(f' baud {baud}', flush=True)
        show('idle', xfer(h, b'', 0.5))
        got = bytearray()
        for _ in range(60):  # C328: SYNC до ACK, 60 попыток
            got += xfer(h, bytes([0xAA, 0x0D, 0, 0, 0, 0]), 0.05)
            if b'\xaa\x0e\x0d' in got:
                break
        show('C328 SYNC', bytes(got))
        show('VC0706 version', xfer(h, bytes([0x56, 0x00, 0x11, 0x00]), 0.5))
        show('VC0706 reset', xfer(h, bytes([0x56, 0x00, 0x26, 0x00]), 1.5))
        k.CloseHandle(h)
print('done', flush=True)
