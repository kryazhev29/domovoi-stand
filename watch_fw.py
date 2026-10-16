"""After flashing: wait for COM5 gone -> back (power cycle), then poll SLDN 101 every 3 s for N seconds.
Port is reopened on every poll, so a USB re-enumeration is survived and logged. Read-only.
  python watch_fw.py 300          status only
  python watch_fw.py 300 --diag   also command 111 (FW 64+): USB watch and main-loop counters
  python watch_fw.py 300 --now    do not wait for a power cycle
Do not filter the output: silence and "no answer" lines are the result."""
import ctypes, struct, sys, time
from ctypes import wintypes
import serial.tools.list_ports as list_ports

args = [a for a in sys.argv[1:] if not a.startswith('--')]
DURATION = float(args[0]) if args else 200
DIAG = '--diag' in sys.argv
k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE
INVALID = wintypes.HANDLE(-1).value
REASONS = {0: '-', 1: 'ESOF', 2: 'IN-stuck', 3: 'unconfigured'}

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)

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

def frames(buf):
    """Yield (cmd, payload) for every CRC-valid SLDN frame."""
    p = buf.find(b'SLDN')
    while p >= 0:
        if len(buf) >= p + 6:
            ln = struct.unpack_from('<H', buf, p + 4)[0]
            if 3 <= ln <= 2048 and len(buf) >= p + 8 + ln and \
               struct.unpack_from('<H', buf, p + 6 + ln)[0] == crc16(buf[p + 4:p + 6 + ln]):
                yield buf[p + 8], bytes(buf[p + 9:p + 6 + ln])
        p = buf.find(b'SLDN', p + 4)

def present():
    return any(c.device == 'COM5' for c in list_ports.comports())

def ask(h, cmd, wait=1.5):
    n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
    fr = sldn(cmd); t = time.monotonic()
    ok = k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    wrote, wtime = n.value, time.monotonic() - t
    buf = bytearray(); end = time.monotonic() + wait
    while ok and time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    return wrote, wtime, buf

def show_status(rel, p):
    up = struct.unpack_from('<I', p, 0)[0]
    extra = f" len={len(p) + 3}" if len(p) != 31 else ""
    log(f"+{rel:5.1f}s 101 time={up} fw={p[6]} csq={p[13]} creg={p[16]} cgreg={p[17]} conn={p[18]} module={p[30]}{extra}")

def show_diag(rel, p):
    if len(p) < 132:
        log(f"+{rel:5.1f}s 111 short payload {len(p)}"); return
    v = struct.unpack_from('<33I', p)
    last = f"{REASONS.get(v[9], v[9])} st={v[10]} pend={v[11]} q={v[12]} in={v[13]} out={v[14]} sof={v[15]}"
    log(f"+{rel:5.1f}s 111 up={v[1]}s esof={v[3]}/{v[4]} busrst={v[5]} recov={v[6]} (in={v[7]} unconf={v[8]}) last[{last}]"
        f" now st={v[16]} pend={v[17]} q={v[18]} in={v[19]} out={v[20]} sof={v[21]}"
        f" txerr={v[22]} rxerr={v[23]} dbgdrop={v[24]} outunconf={v[25]}"
        f" loopmax={v[26]}ms stalls={v[27]} cfgerr={v[28]} gsmon={v[29]} gsmnoans={v[30]} heapfail={v[31]} fault={v[32]}")
    if v[0] >= 2 and len(p) >= 51 * 4:
        r = struct.unpack_from('<51I', p)[33:]
        errs = ""
        if v[0] >= 3 and len(p) >= 53 * 4:
            e = struct.unpack_from('<53I', p)[51:]
            errs = f" buserr={e[0]} buserr_at_recovery={e[1]}"
        if v[0] >= 4 and len(p) >= 57 * 4:
            a = struct.unpack_from('<57I', p)[53:]
            errs += f" daddr_last={a[0]:04x} ep0_last={a[1]:04x} daddr_now={a[2]:04x} ep0_now={a[3]:04x}"
        if v[0] >= 5 and len(p) >= 58 * 4:  # 01.10.2026: минимальный запас стека за работу
            errs += f" stackfree={struct.unpack_from('<58I', p)[57]}"
        log(f"+{rel:5.1f}s 111 regs last[{regs(r[:9])}] now[{regs(r[9:])}]{errs}")

STAT = {0: 'DIS', 1: 'STALL', 2: 'NAK', 3: 'VALID'}

def regs(r):
    """EP1R TX status and CTR_TX: NAK + txstate 1 = lost IN interrupt, VALID = host not polling."""
    ep1, ep3 = r[0], r[1]
    return (f"ep1={ep1:04x} tx={STAT[(ep1 >> 4) & 3]} ctrtx={(ep1 >> 7) & 1} txcnt={r[2]}"
            f" ep3={ep3:04x} rx={STAT[(ep3 >> 12) & 3]} ctrrx={(ep3 >> 15) & 1}"
            f" istr={r[3]:04x} cntr={r[4]:04x} fnr={r[5]:04x} txstate={r[6]} armed={r[7]} taken={r[8]}")

gone = '--now' in sys.argv
if not gone:
    log("waiting for power cycle (COM5 gone -> back)")
end_wait = time.monotonic() + 20 * 60
while time.monotonic() < end_wait:
    p = present()
    if not p and not gone:
        log("COM5 gone"); gone = True
    if p and gone:
        break
    time.sleep(0.1)
else:
    log("timeout waiting for power cycle"); raise SystemExit(1)

t0 = time.monotonic(); log("COM5 present, t=0")
was_present = True; answered = 0; last_ok = None
while time.monotonic() - t0 < DURATION:
    rel = time.monotonic() - t0
    if not present():
        if was_present:
            log(f"+{rel:5.1f}s COM5 GONE"); was_present = False
        time.sleep(0.2); continue
    if not was_present:
        log(f"+{rel:5.1f}s COM5 BACK"); was_present = True
    h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
    if h == INVALID:
        log(f"+{rel:5.1f}s open failed winerror={ctypes.get_last_error()}"); time.sleep(1); continue
    try:
        k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 1000)))
        cmds = [101, 111] if DIAG else [101]
        for cmd in cmds:
            wrote, wtime, buf = ask(h, cmd)
            got = [pl for c, pl in frames(buf) if c == cmd]
            if got:
                if cmd == 101:
                    answered += 1; last_ok = rel; show_status(rel, got[-1])
                else:
                    show_diag(rel, got[-1])
            else:
                log(f"+{rel:5.1f}s {cmd} no answer: wrote {wrote} bytes in {wtime * 1000:.0f} ms, rx={len(buf)}")
    finally:
        k.CloseHandle(h)
    time.sleep(3)
log(f"DONE: answers={answered}, last answer at +{last_ok:.1f}s" if last_ok is not None else "DONE: no answers at all")
