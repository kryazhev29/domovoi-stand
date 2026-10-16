"""Read MCU memory through the old v5 bootloader (v2.1) on COM5. Read-only: 0x00 ident, 0x16 read.
0x16 reads APLICATION_BASE(0x08008000)+offset and repeats the first byte over the whole block,
so memory is read one byte per request (~22 bytes/s).

  boot_read.py flashsize             factory Flash size register 0x1FFFF7E0 (KiB)
  boot_read.py dump ADDRESS SIZE OUT e.g. dump 0x08000000 32768 boot.bin
"""
import ctypes, struct, sys, time
from ctypes import wintypes

BASE = 0x08008000

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE
class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(512)

def exchange(cmd, payload=b'', timeout=3.0):
    b = bytes([0xAA, cmd]) + payload
    fr = b'SLGT' + struct.pack('<H', len(b)) + b + struct.pack('<H', crc16(b))
    if not k.WriteFile(h, fr, len(fr), ctypes.byref(n), None) or n.value != len(fr):
        raise SystemExit(f"cmd 0x{cmd:02x}: write failed, winerror={ctypes.get_last_error()}")
    buf = bytearray(); end = time.monotonic() + timeout
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 512, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        p = buf.find(b'SLGT')
        if p >= 0 and len(buf) >= p + 6:
            ln = struct.unpack_from('<H', buf, p + 4)[0]
            if len(buf) >= p + 8 + ln:
                r = bytes(buf[p + 6:p + 6 + ln])
                if struct.unpack_from('<H', buf, p + 6 + ln)[0] != crc16(r) or r[1] != cmd:
                    raise SystemExit(f"cmd 0x{cmd:02x}: bad answer {buf.hex(' ')}")
                return r[2:]
    raise SystemExit(f"cmd 0x{cmd:02x}: no answer in {timeout} s")

def read_byte(address, retries=3):
    for attempt in range(retries):
        try:
            r = exchange(0x16, struct.pack('<IH', (address - BASE) & 0xFFFFFFFF, 1))
            if r[0] != 0 or struct.unpack_from('<H', r, 1)[0] != 1:
                raise SystemExit(f"read 0x{address:08x}: status {r.hex(' ')}")
            return r[3]
        except SystemExit:
            if attempt == retries - 1:
                raise

try:
    d = exchange(0x00)
    print(f"ident: mode={d[0]} boot=0x{d[1]:02x} hw={d[2]}", flush=True)
    if d[0] != 0 or d[1] not in (0x21, 0x22):
        raise SystemExit("not the old v2.x bootloader, stop")
    if sys.argv[1] == 'flashsize':
        lo, hi = read_byte(0x1FFFF7E0), read_byte(0x1FFFF7E1)
        print(f"Flash size register 0x1FFFF7E0: {lo | hi << 8} KiB", flush=True)
    elif sys.argv[1] == 'dump':
        address, size, out = int(sys.argv[2], 0), int(sys.argv[3], 0), sys.argv[4]
        # An interrupted dump continues from the bytes already saved.
        try:
            data = bytearray(open(out, 'rb').read()[:size])
        except FileNotFoundError:
            data = bytearray()
        if data:
            print(f"resuming at {len(data)} bytes", flush=True)
        t0 = time.monotonic()
        for i in range(len(data), size):
            data.append(read_byte(address + i))
            if i % 1024 == 1023:
                open(out, 'wb').write(data)
                print(f"{i + 1}/{size} ({time.monotonic() - t0:.0f} s)", flush=True)
        open(out, 'wb').write(data)
        print(f"DONE {len(data)} bytes -> {out}", flush=True)
finally:
    k.CloseHandle(h)
