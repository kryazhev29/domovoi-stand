"""Replace the bootloader through the running NEXT firmware, command 110 over USB (COM5).
Protocol (Bootloader/boot_manual.h): op 0 BEGIN(size:u32, crc:u32, hw:u8), 1 DATA(offset:u32, <=256 bytes),
2 COMMIT, 3 CANCEL, 4 STATUS. Reply of command 110: 8 bytes [op, status, received:u32, hw, armed].
Status: 0 OK, 1 BAD, 2 IO, 3 BUSY, 4 DENIED (Flash size), 5 CRC.

  boot_replace.py BOOTLOADER.bin            stage the file, check it, then CANCEL. Nothing is replaced.
  boot_replace.py BOOTLOADER.bin --apply    stage, COMMIT: the firmware rewrites 0x08000000-0x08007FFF
                                            from RAM and resets. Irreversible without SWD.
"""
import ctypes, struct, sys, time
from ctypes import wintypes
from protocol import parse_answer11

path, apply_ = sys.argv[1], '--apply' in sys.argv
image = open(path, 'rb').read()
STATUS = {0: 'OK', 1: 'BAD', 2: 'IO', 3: 'BUSY', 4: 'DENIED', 5: 'CRC'}

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

def crc32(data):
    c = 0xFFFFFFFF
    for (w,) in struct.iter_unpack('<I', data):
        c ^= w
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c

sp, pc = struct.unpack_from('<II', image)
assert len(image) == 32768, 'bootloader file must be 32768 bytes'
# The board does not know its hardware: the HW comes from the file chosen by the board photo.
hw = image[-3]
assert image[-4] == 0x32 and hw in (11, 139) and image[-2:] == bytes([0, 0xFF]), f'tail {image[-4:].hex()} is not version 0x32 / HW 11 or 139'
assert 0x20000008 <= sp <= 0x20005000 and not sp & 7 and pc & 1 and 0x08000000 <= pc & ~1 < 0x08007FFC, 'bad vectors'
crc = crc32(image)
print(f"file ok: HW={hw} SP=0x{sp:08x} PC=0x{pc:08x} CRC=0x{crc:08x}", flush=True)

k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE
class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]
h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 5000)))
# С 18.09.2026 команды по USB, кроме 100/101/102/111, работают только после ПИНа (команда 102).
import os as _os
_b = bytes([255, 1, 102]) + _os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'); _lb = struct.pack('<H', len(_b))
_f = b'SLDN' + _lb + _b + struct.pack('<H', crc16(_lb + _b)); _n = wintypes.DWORD()
k.WriteFile(h, _f, len(_f), ctypes.byref(_n), None); time.sleep(0.3)
k.ReadFile(h, ctypes.create_string_buffer(512), 512, ctypes.byref(_n), None)
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
uid = 0

def frames(buf):
    """Yield (id, payload) of complete CRC-valid SLDN frames and drop them from buf."""
    out = []
    while True:
        p = buf.find(b'SLDN')
        if p < 0 or len(buf) < p + 6:
            return out
        ln = struct.unpack_from('<H', buf, p + 4)[0]
        if len(buf) < p + 8 + ln:
            return out
        body = bytes(buf[p + 6:p + 6 + ln]); ok = struct.unpack_from('<H', buf, p + 6 + ln)[0] == crc16(buf[p + 4:p + 6 + ln])
        del buf[:p + 8 + ln]
        if ok and ln >= 3:
            out.append((body[2], body[3:]))

def send(cmd, payload, want, timeout):
    global uid
    uid = (uid + 1) & 255
    body = bytes([255, uid, cmd]) + payload; lb = struct.pack('<H', len(body))
    fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    if not k.WriteFile(h, fr, len(fr), ctypes.byref(n), None) or n.value != len(fr):
        raise SystemExit(f"cmd {cmd}: write failed, winerror={ctypes.get_last_error()}")
    buf = bytearray(); end = time.monotonic() + timeout
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        for fid, data in frames(buf):
            if fid == 11:
                raise SystemExit(f"cmd {cmd}: firmware answered code {parse_answer11(data, cmd)[0]} (5 = busy/trial, 1 = parameter error)")
            if fid == want:
                return data
    raise SystemExit(f"cmd {cmd}: no answer in {timeout} s")

def manual(op, data=b'', timeout=5.0, expect_ok=True):
    r = send(110, bytes([op]) + data, 110, timeout)
    if len(r) < 8 or r[0] != op:
        raise SystemExit(f"op {op}: bad reply {r.hex(' ')}")
    status, received, hw, armed = r[1], struct.unpack_from('<I', r, 2)[0], r[6], r[7]
    if expect_ok and status:
        raise SystemExit(f"op {op}: status {status} {STATUS.get(status, '?')}, received={received}")
    return status, received, hw, armed

try:
    send(108, b'\x00', 108, 3.0)  # debug log off, also proves the firmware answers
    manual(3)
    t0 = time.monotonic()
    manual(0, struct.pack('<IIB', 32768, crc, hw), timeout=20.0)
    print(f"BEGIN ok, candidate slot erased in {time.monotonic() - t0:.1f} s", flush=True)
    for off in range(0, 32768, 256):
        _, received, _, _ = manual(1, struct.pack('<I', off) + image[off:off + 256])
        assert received == off + 256, (received, off)
        if off % 8192 == 8192 - 256:
            print(f"staged {received}/32768", flush=True)
    if not apply_:
        manual(3)
        print("staged and read back by the firmware; CANCEL sent. Nothing replaced (use --apply).", flush=True)
        sys.exit(0)
    status, received, hw, armed = manual(2, timeout=10.0)
    print(f"COMMIT ok: validated, armed={armed}. Firmware now rewrites the bootloader and resets.", flush=True)
    # On success the RAM writer never returns and the board resets; a second reply means it refused.
    buf = bytearray(); end = time.monotonic() + 8
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        for fid, data in frames(buf):
            if fid == 110 and len(data) >= 2 and data[0] == 2:
                raise SystemExit(f"apply refused: status {data[1]} {STATUS.get(data[1], '?')}; bootloader NOT replaced")
    print("no refusal within 8 s: the board is rewriting the bootloader / resetting", flush=True)
finally:
    k.CloseHandle(h)
