# Доработка: Кряжев А., 2026
"""Эмулятор C328. Windows COM через ctypes; для тестов доступен чистый C328.feed()."""
import argparse
import ctypes as ct
import os
import struct
import time
from pathlib import Path


class C328:
    def __init__(self, jpeg, silent=False, error_packet=-1, delay=0.0):
        if not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
            raise ValueError('Файл должен начинаться FFD8 и заканчиваться FFD9')
        if len(jpeg) > 65535:
            raise ValueError('Для контроллера допустимо не более 65535 байт')
        self.jpeg, self.silent, self.error_packet, self.delay = jpeg, silent, error_packet, delay
        self.packet_size, self.buffer, self.resolution = 64, bytearray(), 7
        self.requests = []

    def feed(self, data):
        self.buffer.extend(data)
        replies = []
        while len(self.buffer) >= 6:
            if self.buffer[0] != 0xAA:
                del self.buffer[0]
                continue
            p = bytes(self.buffer[:6]); del self.buffer[:6]
            self.requests.append(p)
            if self.silent:
                continue
            cmd = p[1]
            ack = bytes([0xAA, 0x0E, cmd, 0, 0, 0])
            if cmd == 0x0D:
                replies.append(ack + b'\xaa\x0d\0\0\0\0')
            elif cmd == 1:
                if p[3] != 7 or p[5] not in (3, 5, 7):
                    replies.append(b'\xaa\x0f\x01\0\0\0')
                else:
                    self.resolution = p[5]; replies.append(ack)
            elif cmd == 6:
                size = p[3] | (p[4] << 8)
                if p[2] != 8 or not 64 <= size <= 512:
                    replies.append(b'\xaa\x0f\x06\0\0\0')
                else:
                    self.packet_size = size; replies.append(ack)
            elif cmd == 5:
                replies.append(ack)
            elif cmd == 4:
                replies.append(ack + b'\xaa\x0a\x01' + len(self.jpeg).to_bytes(3, 'little'))
            elif cmd == 0x0E and p[2] == 0:
                number = p[4] | (p[5] << 8)
                if number == 0xF0F0:
                    continue
                size = self.packet_size - 6
                data = self.jpeg[number * size:(number + 1) * size]
                if not data:
                    replies.append(b'\xaa\x0f\x0e\0\0\0')
                    continue
                packet = struct.pack('<HH', number, len(data)) + data
                check = sum(packet) & 255
                if number == self.error_packet:
                    check ^= 1   # Постоянная ошибка выбранного пакета до таймаута контроллера.
                replies.append(packet + bytes([check, 0]))
        return replies


class WindowsSerial:
    """8N1, без аппаратного управления потоком и без сторонних библиотек."""
    def __init__(self, port, baud):
        if os.name != 'nt':
            raise OSError('COM-транспорт рассчитан на Windows')
        from ctypes import wintypes as w
        self.w = w
        k = self.k = ct.WinDLL('kernel32', use_last_error=True)
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ct.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        for name in ('ReadFile', 'WriteFile'):
            fn = getattr(k, name)
            fn.argtypes = [w.HANDLE, ct.c_void_p, w.DWORD, ct.POINTER(w.DWORD), ct.c_void_p]
            fn.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.SetCommState.argtypes = [w.HANDLE, ct.c_void_p]
        k.GetCommState.argtypes = [w.HANDLE, ct.c_void_p]
        k.SetCommTimeouts.argtypes = [w.HANDLE, ct.c_void_p]
        k.PurgeComm.argtypes = [w.HANDLE, w.DWORD]
        self.handle = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
        if self.handle == ct.c_void_p(-1).value:
            raise ct.WinError(ct.get_last_error())
        class DCB(ct.Structure):
            _fields_ = [('length', w.DWORD), ('baud', w.DWORD), ('flags', w.DWORD),
                        ('reserved', w.WORD), ('xonlim', w.WORD), ('xofflim', w.WORD),
                        ('bytesize', ct.c_ubyte), ('parity', ct.c_ubyte), ('stopbits', ct.c_ubyte),
                        ('xon', ct.c_char), ('xoff', ct.c_char), ('error', ct.c_char),
                        ('eof', ct.c_char), ('event', ct.c_char), ('reserved1', w.WORD)]
        try:
            dcb = DCB(); dcb.length = ct.sizeof(dcb)
            self.check(k.GetCommState(self.handle, ct.byref(dcb)))
            dcb.baud, dcb.flags, dcb.bytesize, dcb.parity, dcb.stopbits = baud, 1, 8, 0, 0
            self.check(k.SetCommState(self.handle, ct.byref(dcb)))
            timeout = (w.DWORD * 5)(0xFFFFFFFF, 0, 50, 0, 1000)
            self.check(k.SetCommTimeouts(self.handle, timeout))
            self.check(k.PurgeComm(self.handle, 0x000F))
        except BaseException:
            self.close(); raise

    @staticmethod
    def check(result):
        if not result:
            raise ct.WinError(ct.get_last_error())

    def read(self):
        b = ct.create_string_buffer(512); n = self.w.DWORD()
        self.check(self.k.ReadFile(self.handle, b, len(b), ct.byref(n), None))
        return b.raw[:n.value]

    def write(self, data):
        while data:
            b = ct.create_string_buffer(data); n = self.w.DWORD()
            self.check(self.k.WriteFile(self.handle, b, len(data), ct.byref(n), None))
            if not n.value:
                raise OSError('COM: таймаут записи')
            data = data[n.value:]

    def close(self):
        if self.handle is not None:
            self.k.CloseHandle(self.handle); self.handle = None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', required=True, help='COM-порт USB-485, например COM8')
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--file', type=Path, required=True)
    p.add_argument('--silent', action='store_true', help='Молчать')
    p.add_argument('--error-packet', type=int, default=-1, help='Портить verify пакета N (нумерация с 0)')
    p.add_argument('--delay', type=float, default=0, help='Задержка каждого ответа, секунды')
    args = p.parse_args()
    if args.baud <= 0 or args.delay < 0:
        p.error('Скорость положительная, задержка неотрицательная')
    camera = C328(args.file.read_bytes(), args.silent, args.error_packet, args.delay)
    port = WindowsSerial(args.port, args.baud)
    print(f'C328 на {args.port}, {args.baud} 8N1, JPEG {len(camera.jpeg)} байт. Ctrl+C — стоп.', flush=True)
    try:
        while True:
            data = port.read()
            for reply in camera.feed(data):
                if camera.delay:
                    time.sleep(camera.delay)
                port.write(reply)
                print(f'{time.strftime("%H:%M:%S")} RX {data.hex(" ")} -> TX {len(reply)} байт', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        port.close()


if __name__ == '__main__':
    main()
