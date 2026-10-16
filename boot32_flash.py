"""Flash HW.FW.BUILD.bin through bootloader 0x32 (SLGT 0x00 / 0x20 / 0x21 / 0x22).
Waits up to 5 min for the board to appear in bootloader mode. Without --go: only waits for ident."""
import ctypes, pathlib, struct, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE
INVALID = wintypes.HANDLE(-1).value

def log(*a): print(time.strftime('%H:%M:%S'), *a, flush=True)

def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8): c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

def crc_stm(d):
    c = 0xFFFFFFFF
    for (w,) in struct.iter_unpack('<I', d):
        c ^= w
        for _ in range(32): c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c

class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]

path = pathlib.Path(sys.argv[1]); go = '--go' in sys.argv; port = 'COM5'
image = path.read_bytes()
assert len(image) % 4 == 0, 'size not multiple of 4'
crc = crc_stm(image)
log(f'{path.name}: {len(image)} bytes, CRC 0x{crc:08x}')
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(256); h = None

def open_port():
    hh = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
    if hh == INVALID: return None
    k.SetCommTimeouts(hh, ctypes.byref(T(20, 0, 50, 0, 2000))); return hh

def ask(cmd, data=b'', wait=2.0):
    body = bytes([0xAA, cmd]) + data
    fr = b'SLGT' + struct.pack('<H', len(body)) + body + struct.pack('<H', crc16(body))
    if not k.WriteFile(h, fr, len(fr), ctypes.byref(n), None): return 'io'
    buf = bytearray(); end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 256, ctypes.byref(n), None) and n.value: buf += tmp.raw[:n.value]
        p = buf.find(b'SLGT\x0a\x00')
        if p >= 0 and len(buf) >= p + 18 and buf[p + 7] == cmd:
            r = buf[p + 6:p + 16]
            if struct.unpack_from('<H', buf, p + 16)[0] == crc16(r):
                return {'status': r[2], 'boot': r[3], 'hw': r[4], 'recv': struct.unpack_from('<I', r, 6)[0]}
    return None

deadline = time.monotonic() + 300
log('waiting for bootloader on', port)
while True:
    if time.monotonic() > deadline: raise SystemExit('no bootloader in 5 min')
    if h is None: h = open_port()
    if h is not None:
        r = ask(0x00, wait=1.0)
        if isinstance(r, dict): break
        if r == 'io': k.CloseHandle(h); h = None
    time.sleep(1)
log(f"bootloader: status {r['status']} boot 0x{r['boot']:02x} hw {r['hw']}")
if r['status'] != 0 or r['hw'] != int(path.name.split('.')[0]):
    raise SystemExit('bootloader refuses or HW mismatch')
if not go:
    k.CloseHandle(h); raise SystemExit('dry run: stop here')
try:
    r = ask(0x20, struct.pack('<II', len(image), crc) + path.name.encode() + b'\0', wait=30.0)
    log('0x20 ->', r)
    if not r or r['status']: raise SystemExit('prepare refused')
    off = 0
    while off < len(image):
        chunk = image[off:off + 256]
        for t in range(5):
            r = ask(0x21, struct.pack('<I', off) + chunk, wait=3.0)
            if r and r['status'] == 0: break
        else: raise SystemExit(f'block at {off} refused: {r}')
        off += len(chunk)
        if off % (256 * 40) == 0 or off == len(image): log(f'{off}/{len(image)}')
    r = ask(0x22, wait=60.0)
    log('0x22 ->', r if r else 'no reply (normal: app started)')
finally:
    k.CloseHandle(h)
