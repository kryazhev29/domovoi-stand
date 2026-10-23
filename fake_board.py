"""Имитатор ДОМОВОЙ NEXT по Протокол2: только TCP, без COM и оборудования.

python fake_board.py --port 15010 [--fault reconnect-open|lost11|hang]
Использует собственные сборщик кадров и CRC, не импортирует тестовый сервер.
"""
import argparse
import socket
import struct
import threading
import time


def crc16(data):
    value = 65535
    for byte in data:
        value ^= byte
        for _ in range(8):
            bit = value & 1
            value >>= 1
            if bit:
                value ^= 40961
    return value


def crc32(data):
    value = 0xffffffff
    for word in struct.iter_unpack('<I', data):
        value ^= word[0]
        for _ in range(32):
            value = ((value << 1) ^ (0x04c11db7 if value & 0x80000000 else 0)) & 0xffffffff
    return value


class FakeBoard:
    def __init__(self, host='127.0.0.1', port=15010, fault=''):
        self.host, self.port, self.fault = host, port, fault
        self.stop = threading.Event()
        self.sock = None
        self.old_sockets = []
        self.buf = bytearray()
        self.uid = 0
        self.registered = False
        self.flags = 0
        self.triggers = bytes(16)
        self.stamps = [0] * 5
        self.tables = {12: bytes([255])*512, 13: bytes([255])*16000,
                       14: bytes([255])*2048, 15: bytes([255])*2048, 16: bytes([255])*1024}
        self.logs = []
        self.log_index = self.log_pending = 0
        self.sync = set()
        self.sync_due = self.out_due = self.reader_due = self.reboot_due = 0
        self.time_offset = 0
        self.fw, self.build = 71, 1791000000
        self.download = None
        self.image = bytearray()
        self.photo = b''
        self.photo_number = 0
        self.ids = {}
        self.next_status = 0

    def now(self):
        return int(time.time() + self.time_offset)

    def emit(self, cmd, payload=b'', uid=None):
        if uid is None:
            self.uid = self.uid % 255 + 1
            uid = self.uid
        content = bytes([255, uid, cmd]) + payload
        body = struct.pack('<H', len(content)) + content
        self.sock.sendall(b'SLDN' + body + struct.pack('<H', crc16(body)))

    def answer(self, cmd, uid, code=0):
        # Протокол2: код, время, свободный буфер; номера команды в ответе 11 нет (f431b74).
        self.emit(11, struct.pack('<BIH', code, int(time.time()), 2048), uid)

    def event(self, kind, arg=b'', passes=255):
        self.log_index += 1
        if 132 <= kind <= 137:
            arg = struct.pack('<BHI', 139, self.fw, self.build)
        raw = struct.pack('<IIB', self.log_index, self.now(), kind) + arg[:10].ljust(10, b'\0')
        self.logs.append(raw + struct.pack('<IB', 0, passes) + bytes(8))

    def status(self):
        flags = self.flags | (0x1000 if self.logs else 0) | (0x2000 if self.photo else 0)
        self.emit(2, struct.pack('<IHH', self.now(), flags, 2048))

    def connect(self):
        old = self.sock
        self.sock = socket.create_connection((self.host, self.port), timeout=2)
        self.sock.settimeout(.05)
        if old:
            if self.fault == 'reconnect-open':
                self.old_sockets.append(old)
            else:
                old.close()
        self.buf.clear()
        self.registered = False
        self.log_pending = 0
        self.emit(1, b'123456789012345' + bytes([1, 139]) + struct.pack('<5I', *self.stamps))

    def command(self, uid, cmd, data):
        if cmd == 1:
            self.registered = True
            if len(data) >= 5:
                self.time_offset = struct.unpack_from('<I', data, 1)[0] - time.time()
            self.event(14)
            self.status()
            self.next_status = time.monotonic() + 15
            return
        if self.fault == 'hang':
            return
        if cmd == 24:
            return
        if self.flags & 0x800 and cmd not in (2, 19, 20, 22, 23, 27):
            self.answer(cmd, uid, 2)
            return
        if cmd == 2:
            self.answer(cmd, uid)
            self.event(33)
            self.reboot_due = time.monotonic() + .1
        elif cmd == 3:
            self.flags = 0
            self.triggers = bytes(16)
            self.ids.clear()
            self.event(0)
            self.answer(cmd, uid)
        elif cmd == 5:
            if len(data) != 16:
                self.answer(cmd, uid, 1)
                return
            for i, on_event in enumerate((30, 28, 24, 26)):
                if data[i] != self.triggers[i]:
                    self.event(on_event if data[i] else on_event+1)
            self.triggers = data
            self.flags = self.flags & ~0xf0 | sum(bool(data[i]) << (7-i) for i in range(4))
            self.answer(cmd, uid)
            self.status()
        elif cmd == 6:
            self.answer(cmd, uid, 0 if len(data) == 3 and data[0] else 1)
            if len(data) == 3 and data[0]:
                self.reader_due = time.monotonic() + data[0]
        elif cmd == 7:
            self.answer(cmd, uid)
            self.photo_number = max(self.photo_number+1, self.now())
            self.photo = b'\xff\xd8' + bytes(range(256))*9 + b'\xff\xd9'
            self.event(129, struct.pack('<I', self.photo_number))
            self.status()
        elif cmd == 8:
            if len(data) not in (3, 13) or data[1] > 1 or data[2] > 1:
                self.answer(cmd, uid, 1)
                return
            if self.fault in ('output-blocked', 'output-busy'):
                if self.fault == 'output-busy':
                    self.flags |= 2
                    self.status()
                self.answer(cmd, uid, 2)
                self.status()
                return
            if self.flags & 2:
                self.answer(cmd, uid, 2)
                return
            ident = data[3:13]
            if ident in self.ids:
                if not self.ids[ident]:
                    self.answer(cmd, uid, 2)
                    self.status()
                    return
                self.ids[ident] -= 1
                self.event(131, ident, self.ids[ident])
                if not self.ids[ident]:
                    self.event(79, ident, 0)
            self.answer(cmd, uid)
            self.event({1: 77, 2: 75, 3: 74, 4: 76}.get(data[0], 75))
            self.flags |= 2
            self.status()
            self.out_due = time.monotonic() + .2
        elif cmd == 9:
            if self.fault == 'journal-hang':
                return
            del self.logs[:self.log_pending]
            if self.fault == 'journal-infinite':
                self.event(14)
            rows = self.logs[:20]
            self.log_pending = len(rows)
            self.emit(10, struct.pack('<HB', len(self.logs)-len(rows), len(rows)) + b''.join(rows), uid)
        elif cmd == 10:
            self.emit(9, bytes(3), uid)
        elif cmd == 11:
            self.answer(cmd, uid, 0 if len(data) >= 11 else 1)
            self.event(36)
        elif cmd in (12, 13, 14, 15, 16):
            kind = {12: 4, 13: 1, 14: 3, 15: 2, 16: 5}[cmd]
            if len(data) < (4 if cmd == 12 else 5):
                self.answer(cmd, uid, 1)
                return
            stamp = struct.unpack_from('<I', data)[0]
            if cmd == 12:
                self.tables[cmd] = data
            elif cmd == 16:
                count, dates = data[4], data[5:]
                if not count or len(dates) != count*4:
                    self.answer(cmd, uid, 1)
                    return
                tail = self.tables[cmd][len(dates):] if self.fault == 'holiday-tail' else bytes([255])*(1024-len(dates))
                self.tables[cmd] = dates + tail
            else:
                size = {13: 32, 14: 32, 15: 16}[cmd]
                if len(data) != 5+data[4]*size:
                    self.answer(cmd, uid, 1)
                    return
                table = bytearray(self.tables[cmd])
                for i in range(data[4]):
                    rec = data[5+i*size:5+(i+1)*size]
                    cell = int.from_bytes(rec[:2] if cmd == 13 else rec[:1], 'little')
                    table[(cell-1)*size:cell*size] = rec
                    if cmd == 13:
                        self.ids[rec[2:12]] = rec[20]
                self.tables[cmd] = bytes(table)
            self.stamps[kind-1] = stamp
            self.sync.add({12: 7, 13: 6, 14: 9, 15: 8, 16: 10}[cmd])
            self.sync_due = time.monotonic()+30
            self.emit(12, struct.pack('<BIH', kind, stamp, crc16(self.tables[cmd])), uid)
        elif cmd == 17:
            self.emit(5, struct.pack('<5H', *(crc16(self.tables[n]) for n in (13, 15, 14, 12, 16))), uid)
        elif cmd == 18:
            if len(data) != 1 or data[0] > 100:
                self.answer(cmd, uid, 1)
            elif data[0] == 0:
                self.emit(6, struct.pack('<100H', *([crc16(self.tables[13])]*100)), uid)
            else:
                self.emit(15, data + struct.pack('<H', crc16(self.tables[13])), uid)
        elif cmd == 19:
            if len(data) != 16:
                self.answer(cmd, uid, 1)
                return
            self.download = struct.unpack('<HIIIH', data)
            self.image.clear()
            self.flags |= 0x800
            self.event(132)
            if self.fault != 'lost11':
                self.answer(cmd, uid)
            self.status()
            if self.fault != 'no-progress':
                self.emit(13, bytes(2))
        elif cmd == 20:
            if not self.download:
                self.emit(4, bytes([0, 253]), uid)
                return
            index = int.from_bytes(data[:2], 'little')
            if len(data) != 1026 or index*1024 != len(self.image):
                self.emit(4, bytes([0, 255]), uid)
                return
            self.image.extend(data[2:])
            blocks = self.download[4]
            self.emit(4, bytes([(index+1)*100//blocks, 0]), uid)
            if index+1 < blocks:
                self.emit(13, struct.pack('<H', index+1))
            else:
                if crc32(self.image[:self.download[2]]) != self.download[3]:
                    self.emit(4, bytes([100, 253]), uid)
                    return
                self.fw, self.build = self.download[:2]
                if self.fault == 'wrong-build':
                    self.build -= 1
                self.event(135)
                if self.fault != 'missing-event':
                    self.event(136)
                self.download = None
                self.reboot_due = time.monotonic()+.2
        elif cmd == 21:
            if len(data) != 6:
                self.answer(cmd, uid, 1)
                return
            number, off = struct.unpack('<IH', data)
            if not self.photo:
                # Повторное итоговое подтверждение пустой очереди допустимо.
                self.answer(cmd, uid)
                self.emit(14, bytes(1))
                return
            if number and number != self.photo_number or off > len(self.photo):
                self.answer(cmd, uid, 1)
                return
            self.answer(cmd, uid)
            if number and off == len(self.photo):
                self.photo = b''
            if not self.photo:
                self.emit(14, bytes(1))
            else:
                for pos in range(off, len(self.photo), 1024):
                    self.emit(14, bytes([1])+struct.pack('<IIH', self.photo_number, len(self.photo), pos)+self.photo[pos:pos+1024])
                self.emit(14, bytes(1))
        elif cmd == 22:
            self.emit(3, struct.pack('<IBHBIB', self.now(), 23, 250, self.fw, self.build, 0x32)+bytes(8), uid)
        elif cmd == 23:
            if len(data) != 4:
                self.answer(cmd, uid, 1)
            else:
                self.time_offset = int.from_bytes(data, 'little')-time.time()
                self.answer(cmd, uid)
                self.status()
        elif cmd in (25, 27):
            self.answer(cmd, uid)
        else:
            self.answer(cmd, uid, 0 if self.fault == 'unknown99' else 255)

    def tick(self):
        now = time.monotonic()
        if self.reboot_due and now >= self.reboot_due:
            self.reboot_due = 0
            self.flags &= ~0x800
            self.connect()
        if self.fault == 'hang':
            return
        if self.out_due and now >= self.out_due:
            self.out_due = 0; self.flags &= ~2; self.status()
        if self.reader_due and now >= self.reader_due:
            self.reader_due = 0; self.emit(8, bytes([255])+bytes(10))
        if self.sync_due and now >= self.sync_due:
            self.sync_due = 0
            for kind in sorted(self.sync):
                self.event(kind)
            self.sync.clear()
            self.status()
        if self.registered and now >= self.next_status:
            self.next_status = now+15
            self.status()

    def run(self):
        deadline = time.monotonic()+15
        while not self.stop.is_set():
            try:
                self.connect()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                self.stop.wait(.1)
        try:
            while not self.stop.is_set():
                self.tick()
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.buf.extend(data)
                while len(self.buf) >= 6:
                    if self.buf[:4] != b'SLDN':
                        del self.buf[0]
                        continue
                    n = int.from_bytes(self.buf[4:6], 'little')
                    if not 3 <= n <= 2048:
                        del self.buf[0]
                        continue
                    if len(self.buf) < n+8:
                        break
                    packet = bytes(self.buf[:n+8])
                    del self.buf[:n+8]
                    if crc16(packet[4:-2]) == int.from_bytes(packet[-2:], 'little'):
                        self.command(packet[7], packet[8], packet[9:-2])
        finally:
            for sock in [self.sock, *self.old_sockets]:
                if sock:
                    sock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=15010)
    parser.add_argument('--fault', default='', choices=['', 'hang', 'reconnect-open', 'lost11',
                        'no-progress', 'wrong-build', 'missing-event', 'holiday-tail', 'unknown99',
                        'output-blocked', 'output-busy', 'journal-hang', 'journal-infinite'])
    args = parser.parse_args()
    FakeBoard(args.host, args.port, args.fault).run()
