"""Диагностика новой связи: команда 112 по USB (только чтение). Доработка: Кряжев А., 2026.
Запуск: python next_comm.py [COM5] [--loop N] — N раз с паузой 5 с."""
import ctypes, struct, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True); k.CreateFileW.restype = wintypes.HANDLE

STEPS = ['DISCHARGE', 'POWER_LEAD', 'POWER_RAIL', 'POWER_KEY', 'POWER_BOOT', 'AT_READY', 'MODEL', 'IMEI',
         'ECHO', 'CMEE', 'IFC', 'CLIP', 'CMER', 'CREG_URC', 'CGREG_URC', 'CEREG_URC', 'URC_PORT', 'URC_SET',
         'SIM', 'RAT_QUERY', 'RAT_SET', 'NETWORK', 'IMSI', 'APN', 'DEACT', 'ACTIVATE', 'TCP', 'READ']
PHASES = ['POWER', 'SETUP', 'SIM', 'NETWORK', 'PDP', 'TCP']
MODEM = ['IDLE', 'STARTING', 'READY', 'FAILED']
REASON = ['NONE', 'TIMEOUT', 'AT', 'RESPONSE', 'NO_SIM', 'PIN', 'PUK', 'NETWORK', 'PDP', 'TCP', 'STREAM',
          'NO_ANSWER', 'TYPE']
CHANNEL = ['INITIAL', 'OPEN', 'CLOSE', 'CLOSING', 'VERIFY', 'PAUSE', 'OPENING', 'FAULT']
SERVER = ['OFF', 'REG', 'REG_WAIT', 'ONLINE', 'PING', 'DNS', 'SILENT']


def name(table, i):
    return table[i] if i < len(table) else str(i)


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


port = next((a for a in sys.argv[1:] if a.upper().startswith('COM')), 'COM5')
loops = int(sys.argv[sys.argv.index('--loop') + 1]) if '--loop' in sys.argv else 1
h = k.CreateFileW('\\\\.\\' + port, 0xC0000000, 0, None, 3, 0, None)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f'open failed, winerror={ctypes.get_last_error()}')
k.SetCommTimeouts(h, ctypes.byref(T(50, 0, 100, 0, 2000)))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)


def ask(cmd, wait=1.5):
    fr = sldn(cmd); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    buf = bytearray(); end = time.monotonic() + wait
    while time.monotonic() < end:
        if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value:
            buf += tmp.raw[:n.value]
    return bytes(buf)


try:
    for loop in range(loops):
        if loop:
            time.sleep(5)
        buf = ask(112)
        p = buf.rfind(b'SLDN')
        while p >= 0 and (len(buf) < p + 9 or buf[p + 8] != 112):
            p = buf.rfind(b'SLDN', 0, p)
        if p < 0:
            print('no answer to 112:', buf[-80:].hex(' '))
            continue
        length = struct.unpack_from('<H', buf, p + 4)[0]
        if len(buf) < p + 8 + length:
            print('short answer'); continue
        ok = struct.unpack_from('<H', buf, p + 6 + length)[0] == crc16(buf[p + 4:p + 6 + length])
        v = struct.unpack_from('<%dI' % ((length - 3) // 4), buf, p + 9)
        steps_table = STEPS if len(v) - 25 == 28 else STEPS[:26] + ['DATA_ECHO'] + STEPS[26:]
        st, step, phase, reason = v[2] & 255, v[2] >> 8 & 255, v[2] >> 16 & 255, v[2] >> 24
        ch, sv, rc = v[5], v[6], v[7]
        print(time.strftime('%H:%M:%S'), f'crc={ok} ms={v[1]}')
        print(f'  modem {name(MODEM, st)} step={name(steps_table, step)} phase={name(PHASES, phase)} reason={name(REASON, reason)}'
              f' code={v[3] & 0xFFFF} at_fault={v[3] >> 16} done_steps=0x{v[4]:08x}')
        print(f'  channel {name(CHANNEL, ch & 255)} tx={ch >> 8 & 255} op={ch >> 16 & 255} conn={ch >> 24}'
              f' | server {name(SERVER, sv & 255)} misses={sv >> 8 & 255} reg={sv >> 16 & 255}')
        print(f'  recover {"WAIT" if rc & 15 else "WATCH"} power={rc >> 4 & 15} power_step={rc >> 8 & 255}'
              f' soft_step={rc >> 16 & 255} net_step={rc >> 24} wait_left={v[8]} ms')
        print(f'  uart errors={v[9]} overflow={v[10]} ore={v[11]} fe={v[12]} ne={v[13]}')
        count, nxt, last = v[14] & 255, v[14] >> 8 & 255, v[14] >> 16
        ring = []
        for i in range(min(count, 10)):
            e = v[15 + (nxt - 1 - i) % 10]
            ring.append(f'{name(steps_table, e & 255)}/{name(REASON, e >> 8 & 255)}@{e >> 16}s')
        print(f'  failures={count} last_code={last}: ' + ', '.join(ring))
        steps = v[25:]
        print('  steps(10ms): ' + ' '.join(f'{name(steps_table, i)}={t}' for i, t in enumerate(steps) if t))
finally:
    k.CloseHandle(h)
