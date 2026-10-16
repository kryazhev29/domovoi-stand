"""Flash a legacy SLFW package through the old v5 bootloader (v2.1) on COM5.
Sequence mirrors ConfiguratorDomovoi: 0x00 ident, 0x10 unlock, 0x12 erase, 0x13 header, 0x14 x N, 0x15 CRC, 0x11 lock.
No 0x04 reset: the user power-cycles the board. Without --go only the offline model check runs."""
import ctypes, struct, sys, time
from ctypes import wintypes

pkg_path, go = sys.argv[1], '--go' in sys.argv
pkg = open(pkg_path, 'rb').read()
header, body = pkg[:512], pkg[512:]

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

def stm_crc(data):
    crc = 0xFFFFFFFF
    for (w,) in struct.iter_unpack('<I', data):
        crc ^= w
        for _ in range(32):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc

# offline model of bootloader.c: 0x13 stores header, 0x14 decodes each 1024-byte block with a chained key
ident, hw, fw = struct.unpack_from('<IBB', header)
size, crc = struct.unpack_from('<II', header, 8)
assert ident == 0x57464C53 and len(body) == size and size % 4 == 0 and size + 512 < 0x18000, 'bad package'
key, image = 0x5A, bytearray()
for off in range(0, len(body), 1024):
    for b in body[off:off + 1024]:
        p = ((b ^ key) + fw) & 255
        image.append(p); key = (key + p) & 255
assert stm_crc(bytes(image)) == crc, 'model CRC mismatch'
sp, pc = struct.unpack_from('<II', image)
print(f"model ok: HW={hw} FW={fw} size={size} CRC=0x{crc:08x} SP=0x{sp:08x} PC=0x{pc:08x} end=0x{0x08008200 + size - 1:08x}", flush=True)
if not go:
    sys.exit(0)

k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE
class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 5000)))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(512)

def exchange(cmd, payload=b'', timeout=5.0):
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
                if struct.unpack_from('<H', buf, p + 6 + ln)[0] != crc16(r):
                    raise SystemExit(f"cmd 0x{cmd:02x}: bad CRC in answer {buf.hex(' ')}")
                if r[1] != cmd:
                    raise SystemExit(f"cmd 0x{cmd:02x}: unexpected answer id 0x{r[1]:02x} data {r[2:].hex(' ')}")
                return r[2:]
    raise SystemExit(f"cmd 0x{cmd:02x}: no answer in {timeout} s")

def ok(cmd, payload=b'', timeout=5.0):
    r = exchange(cmd, payload, timeout)
    if r[:1] != b'\x00':
        raise SystemExit(f"cmd 0x{cmd:02x}: bootloader status {r.hex(' ')}")

try:
    d = exchange(0x00)
    print(f"ident: mode={d[0]} boot=0x{d[1]:02x} hw={d[2]}", flush=True)
    if d[0] != 0 or d[1] not in (0x21, 0x22):
        raise SystemExit("not the old v2.x bootloader, stop")
    ok(0x10); print("unlocked", flush=True)
    t0 = time.monotonic()
    ok(0x12, struct.pack('<I', size + 512), 20); print(f"erased in {time.monotonic() - t0:.1f} s", flush=True)
    ok(0x13, struct.pack('<H', 512) + header); print("header written", flush=True)
    t0 = time.monotonic()
    for off in range(0, len(body), 1024):
        chunk = body[off:off + 1024]
        ok(0x14, struct.pack('<IH', off, len(chunk)) + chunk)
        if (off // 1024) % 8 == 7 or off + 1024 >= len(body):
            print(f"written {off + len(chunk)}/{len(body)} ({time.monotonic() - t0:.0f} s)", flush=True)
    ok(0x15); print("CRC check on board: OK", flush=True)
    ok(0x11); print("locked. DONE", flush=True)
finally:
    k.CloseHandle(h)
