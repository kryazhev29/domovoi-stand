"""Debug log (SLDN 108) with the host time of every received chunk, from power-on.
  python log_timed.py SECONDS BITS FILE [--now] [--ping]
BITS: 1 modem AT exchange, 2 GSM events, 4 general events (7 = all).
Without --now waits for COM5 to disappear and come back (power cycle), then enables the log
at once: the 108 setting lives in RAM and is 0 after every start.
The port stays open for the whole run. At the end the log is switched off and the queue drained:
closing the port with the log on blocks the firmware until the watchdog resets it.
Output lines: "+seconds_since_COM5  text". --ping sends status 101 every 3 s so the firmware USB watch can recover a stalled IN pipe.
Read-only apart from the RAM log switch."""
import ctypes, struct, sys, time
from ctypes import wintypes
import serial.tools.list_ports as list_ports

args = [a for a in sys.argv[1:] if not a.startswith('--')]
seconds, bits, out = float(args[0]), int(args[1]), args[2]
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

def sldn(cmd, payload=b''):
    body = bytes([255, 1, cmd]) + payload; lb = struct.pack('<H', len(body))
    return b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))

def present():
    return any(p.device == 'COM5' for p in list_ports.comports())

if '--now' not in sys.argv:
    print(time.strftime('%H:%M:%S'), 'waiting for power cycle', flush=True)
    while present(): time.sleep(0.05)
    print(time.strftime('%H:%M:%S'), 'COM5 gone', flush=True)
    while not present(): time.sleep(0.05)
t0 = time.monotonic()
print(time.strftime('%H:%M:%S'), 'COM5 back', flush=True)
h = wintypes.HANDLE(-1).value
while time.monotonic() - t0 < 10:
    h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
    if h != wintypes.HANDLE(-1).value: break
    time.sleep(0.05)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"open failed, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
f = open(out, 'w', encoding='cp1251', errors='replace', newline='\n')
partial = ''
def feed(chunk, stamp):
    global partial
    text = partial + chunk.decode('cp1251', errors='replace')
    lines = text.split('\n')
    partial = lines.pop()
    for line in lines:
        f.write(f"+{stamp:8.3f}  {line.rstrip(chr(13))}\n")
    f.flush()
try:
    fr = sldn(108, bytes([bits]))
    k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    f.write(f"+{time.monotonic() - t0:8.3f}  [host] log bits {bits} enabled\n")
    end = t0 + seconds
    ping = sldn(101); next_ping = time.monotonic() + 3
    while time.monotonic() < end:
        ok = k.ReadFile(h, tmp, 4096, ctypes.byref(n), None)
        if ok and n.value:
            feed(tmp.raw[:n.value], time.monotonic() - t0)
        if not ok:
            # The USB watch re-enumerated the device: the old handle is dead. Reopen.
            f.write(f"+{time.monotonic() - t0:8.3f}  [host] read failed winerror={ctypes.get_last_error()}, reopening\n")
            k.CloseHandle(h); h = wintypes.HANDLE(-1).value
            while time.monotonic() < end and h == wintypes.HANDLE(-1).value:
                time.sleep(0.2)
                h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
            if h == wintypes.HANDLE(-1).value: break
            k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
            k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
            f.write(f"+{time.monotonic() - t0:8.3f}  [host] reopened, log bits {bits} sent again\n")
            continue
        if '--ping' in sys.argv and time.monotonic() >= next_ping:
            # A host write arms the firmware USB watch, so a stalled IN pipe recovers
            # instead of silencing the rest of the log.
            k.WriteFile(h, ping, len(ping), ctypes.byref(n), None)
            next_ping = time.monotonic() + 3
finally:
    fr = sldn(108, b'\x00'); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    end = time.monotonic() + 2
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            feed(tmp.raw[:n.value], time.monotonic() - t0)
    if partial:
        f.write(f"+{time.monotonic() - t0:8.3f}  {partial}\n")
    f.write(f"+{time.monotonic() - t0:8.3f}  [host] log off, port closed\n")
    f.close()
    k.CloseHandle(h)
print(time.strftime('%H:%M:%S'), 'done', out, flush=True)
