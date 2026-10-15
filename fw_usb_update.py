"""Update the application over USB with commands 19/20, as NextConfig does.
Usage: py -3.11 fw_usb_update.py <HW.FW.BUILD.bin> [COM5]
Works with the firmware of 16.09 (percent 248 at the end) and with the
Protokol2 firmware (code 0 for every block, 253 stops)."""
import ctypes, pathlib, struct, sys, time
from protocol import parse_answer11
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE

def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c

def crc_stm(data):
    c = 0xFFFFFFFF
    for i in range(0, len(data), 4):
        c ^= struct.unpack_from('<I', data, i)[0]
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c

class T(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in ('a', 'b', 'c', 'd', 'e')]

path = pathlib.Path(sys.argv[1]); port = sys.argv[2] if len(sys.argv) > 2 else 'COM5'
hw, fw, build = (int(x) for x in path.name.split('.')[:3])
image = path.read_bytes(); image += b'\xff' * (-len(image) % 4)
blocks = (len(image) + 1023) // 1024
h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
# С 18.09.2026 команды по USB, кроме 100/101/102/111, работают только после ПИНа (команда 102).
import os as _os
_b = bytes([255, 1, 102]) + _os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:4].ljust(4, b'\0'); _lb = struct.pack('<H', len(_b))
_f = b'SLDN' + _lb + _b + struct.pack('<H', crc16(_lb + _b)); _n = wintypes.DWORD()
k.WriteFile(h, _f, len(_f), ctypes.byref(_n), None); time.sleep(0.3)
k.ReadFile(h, ctypes.create_string_buffer(512), 512, ctypes.byref(_n), None)
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096); uniq = 0

def ask(cmd, payload, want, wait):
    global uniq
    uniq = (uniq + 1) & 255
    body = bytes([255, uniq, cmd]) + payload; lb = struct.pack('<H', len(body))
    fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
    k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
        p = 0
        while (p := buf.find(b'SLDN', p)) >= 0 and len(buf) >= p + 6:
            ln = struct.unpack_from('<H', buf, p + 4)[0]
            if len(buf) < p + 8 + ln: break
            if buf[p + 7] == uniq and buf[p + 8] == want and struct.unpack_from('<H', buf, p + 6 + ln)[0] == crc16(buf[p + 4:p + 6 + ln]):
                return bytes(buf[p + 9:p + 6 + ln])
            p += 4
    return None

try:
    ask(108, b'\x00', 255, 1.0)                      # debug log off
    a = ask(19, struct.pack('<HIIIH', fw, build, len(image), crc_stm(image), blocks), 11, 8.0)
    print('19 ->', a.hex(' ') if a else 'no answer')
    if not a or parse_answer11(a, 19)[0] not in (0, 5): raise SystemExit('refused')
    k_, tries = 0, 0
    while k_ < blocks:
        data = image[k_ * 1024:(k_ + 1) * 1024].ljust(1024, b'\xff')
        r = ask(20, struct.pack('<H', k_) + data, 4, 3.0)
        pct, code = (r[0], r[1]) if r else (None, None)
        if pct == 248: k_ = blocks; print('old firmware: all blocks taken (248)'); break
        if pct is not None and pct > 100 and pct != 255: raise SystemExit(f'stopped, progress code {pct}')
        if code == 0 and pct is not None and pct <= 100:
            k_ += 1; tries = 0
            if k_ % 10 == 0 or k_ == blocks: print(f'block {k_}/{blocks} {pct}%')
            continue
        if code == 253: raise SystemExit('controller stopped the download (253)')
        tries += 1
        if tries >= 5: raise SystemExit(f'block {k_} refused 5 times ({pct}, {code})')
    print('done: the controller checks the image and restarts to install')
finally:
    k.CloseHandle(h)
