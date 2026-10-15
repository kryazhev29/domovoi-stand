"""Roll the application back to the backup copy over USB: command 19 with number 1.
Usage: py -3.11 fw_usb_rollback.py [COM5]. Answer 11: 0 started, 2 not now
(no backup, trial, download); the reason is in the event journal (event 5)."""
import ctypes, struct, sys, time
from ctypes import wintypes
from protocol import take_frames, parse_answer11
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

port = sys.argv[1] if len(sys.argv) > 1 else 'COM5'
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
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
try:
    for cmd, payload in ((108, b'\x00'), (19, struct.pack('<HIIIH', 1, 0, 0, 0, 0))):
        body = bytes([255, 7, cmd]) + payload; lb = struct.pack('<H', len(body))
        fr = b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))
        k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
        buf = bytearray(); end = time.monotonic() + (8.0 if cmd == 19 else 1.0)
        while time.monotonic() < end:
            if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
                buf += tmp.raw[:n.value]
            packets, buf = take_frames(buf)
            answer = next((p for uid, c, p in packets if uid == 7 and c == 11), None)
            if cmd == 19 and answer is not None:
                print('19/1 -> answer 11, code', parse_answer11(answer, 19)[0]); break
        else:
            if cmd == 19: print('19/1 -> no answer')
finally:
    k.CloseHandle(h)
