"""USB-наблюдение за платой: журнал 108/109 с разбором кадров + статус 101 раз в 2 с.
  py -3.11 usb_watch.py СЕКУНД БИТЫ ФАЙЛ_БЕЗ_РАСШИРЕНИЯ
Пишет ФАЙЛ.txt (разобранные строки) и ФАЙЛ.bin (сырой поток). COM5 должен быть свободен."""
import ctypes, struct, sys, time
from ctypes import wintypes
sys.stdout.reconfigure(encoding='utf-8')
seconds, bits, out = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
ping_every = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
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

EVENTS = {0: 'заводские настройки', 6: 'синхр. ID', 7: 'синхр. настроек', 8: 'синхр. групп', 9: 'синхр. расписаний',
          10: 'синхр. праздников', 11: 'нет GSM', 12: 'нет GPRS', 13: 'нет трафика', 14: 'подключён к серверу 1',
          15: 'подключён к серверу 2', 16: 'ошибка сервера 1', 17: 'ошибка сервера 2', 18: 'отказ сервера 1', 19: 'отказ сервера 2',
          20: 'вход 1 вкл', 21: 'вход 1 выкл', 22: 'вход 2 вкл', 23: 'вход 2 выкл', 24: 'авторег вкл', 25: 'авторег выкл',
          26: 'открывать всем вкл', 27: 'открывать всем выкл', 28: 'разблок. сцена вкл', 29: 'разблок. сцена выкл',
          30: 'разблок. ручная вкл', 31: 'разблок. ручная выкл', 32: 'перезагрузка по таймеру', 33: 'перезагрузка по команде',
          34: 'перезагрузка по расписанию', 35: 'принята SMS', 36: 'отправлена SMS', 37: 'низкий баланс', 38: 'слабый сигнал',
          39: 'сброс по питанию', 40: 'причина сброса',
          65: 'ID нет в базе', 66: 'ID заблокирован', 67: 'ID добавлен по SMS', 68: 'ID добавлен авторег', 69: 'выход по звонку',
          70: 'выход по RFID', 71: 'выход по Wiegand', 72: 'выход по RF433', 73: 'выход по авто', 74: 'выход: тест',
          75: 'выход: API', 76: 'выход: голос', 77: 'выход: приложение', 78: 'ID со считывателя', 79: 'лимит проходов',
          129: 'фото', 131: 'счётчик проходов', 132: 'FW начало', 133: 'FW сбой', 134: 'FW таймаут', 135: 'FW установка',
          136: 'FW готово', 137: 'FW откат', 138: 'срок ID истёк', 139: 'срок ID начат'}
AT_RESULT = {0: 'OK', 1: 'ERROR', 2: 'CME', 3: '>', 4: 'таймаут', 5: 'переполнение строки', 6: 'ошибка UART',
             7: 'плохой кадр', 8: 'плохая длина', 9: 'приёмник', 10: 'SEND FAIL'}
PHASES = {0: 'питание', 1: 'настройка', 2: 'SIM', 3: 'сеть', 4: 'PDP', 5: 'TCP'}

t0 = time.monotonic()
f = open(out + '.txt', 'w', encoding='utf-8', newline='\n')
raw = open(out + '.bin', 'wb')
last_status = None
text_tail = {}

def say(line):
    s = f"+{time.monotonic() - t0:7.2f} {line}"
    print(s, flush=True); f.write(s + '\n'); f.flush()

def txt(b):
    return b.decode('cp1251', errors='replace')

def on_log(body):
    kind, ms, lost = body[0], struct.unpack_from('<I', body, 1)[0], struct.unpack_from('<H', body, 5)[0]
    payload = body[7:]
    cont, kind = kind & 128, kind & 127
    lost_s = f" [потеряно {lost}]" if lost else ''
    if kind in (1, 2, 3):
        acc = text_tail.get(kind, '') + txt(payload)
        if cont:
            text_tail[kind] = acc; return
        text_tail.pop(kind, None)
        tag = {1: 'AT>', 2: 'AT<', 3: 'URC'}[kind]
        say(f"{ms:8d} {tag} {acc}{lost_s}")
    elif kind == 4:
        code = payload[0]; val = struct.unpack_from('<H', payload, 1)[0] if len(payload) >= 3 else 0
        say(f"{ms:8d} AT= {AT_RESULT.get(code, code)}{(' код '+str(val)) if val else ''}{lost_s}")
    elif kind == 5:
        code = payload[0]; val = struct.unpack_from('<H', payload, 1)[0] if len(payload) >= 3 else 0
        say(f"{ms:8d} данные {'TX' if code else 'RX'} {val} Б{lost_s}")
    elif kind == 16 and len(payload) >= 7:
        dur = struct.unpack_from('<I', payload, 2)[0]
        say(f"{ms:8d} ЭТАП {PHASES.get(payload[6], payload[6])} -> {PHASES.get(payload[0], payload[0])} за {dur} мс, причина {payload[1]}{lost_s}")
    elif kind == 17 and len(payload) >= 5:
        say(f"{ms:8d} СЕРВЕР код {payload[0]} адрес {struct.unpack_from('<I', payload, 1)[0]}{lost_s}")
    elif kind == 18 and len(payload) >= 5:
        say(f"{ms:8d} ПЕРЕЗАПУСК модема: питание {payload[0]}, пауза {struct.unpack_from('<I', payload, 1)[0]} мс{lost_s}")
    elif kind == 19:
        say(f"{ms:8d} сеть: снимок {len(payload)} Б{lost_s}")
    elif kind == 32 and len(payload) >= 32:
        idx, dt, typ = struct.unpack_from('<IIB', payload, 0)
        ident = payload[9:19]; cnt = payload[23]
        if typ == 129: ident_s = f"фото {struct.unpack_from('<I', ident, 0)[0]}"
        elif typ in (132, 133, 134, 135, 136, 137): ident_s = f"hw {ident[0]} fw {struct.unpack_from('<H', ident, 1)[0]} билд {struct.unpack_from('<I', ident, 3)[0]}"
        else: ident_s = ident.split(b'\0')[0].decode('ascii', errors='replace')
        say(f"{ms:8d} СОБЫТИЕ {typ} ({EVENTS.get(typ, '?')}) id={ident_s} cnt={cnt} t={dt}{lost_s}")
    else:
        say(f"{ms:8d} вид {kind}: {payload.hex()}{lost_s}")

def on_status(body):
    global last_status
    # body[0]=255/0,[1]=uniq,[2]=101,[3..6]=time,[7..8]=status,[9]=fw,[10..13]=build,[14]=boot,[15]=hw,[16]=csq,
    # [17..18]=balance,[19]=creg,[20]=cgreg,[21]=connected,[22]=net,[23..30]=periph,[31..32]=ids,[33]=module
    if len(body) < 34: return
    dt, st = struct.unpack_from('<IH', body, 3)
    build = struct.unpack_from('<I', body, 10)[0]
    csq, conn, net = body[16], body[21], body[22]
    ids = struct.unpack_from('<H', body, 31)[0]
    out1, out2 = (st >> 1) & 1, st & 1
    cur = (st, conn, ids, out1, out2, net)
    if cur != last_status:
        say(f"СТАТУС 0x{st:04x} вых1={out1} вых2={out2} вх1={(st>>3)&1} вх2={(st>>2)&1} фото={(st>>13)&1} журнал={(st>>12)&1} связь={conn} сеть={net} csq={csq} ID={ids} билд={build} t={dt}")
        last_status = cur

buf = b''
def feed(chunk):
    global buf
    raw.write(chunk); raw.flush()
    buf += chunk
    while True:
        i = buf.find(b'SLDN')
        if i < 0:
            buf = buf[-3:]; return
        if i: buf = buf[i:]
        if len(buf) < 6: return
        n = struct.unpack_from('<H', buf, 4)[0]
        if n < 3 or n > 2048:
            buf = buf[1:]; continue
        if len(buf) < n + 8: return
        frame = buf[:n + 8]
        if struct.unpack_from('<H', frame, n + 6)[0] != crc16(frame[4:n + 6]):
            buf = buf[1:]; continue
        buf = buf[n + 8:]
        body = frame[6:n + 6]
        cmd = body[2]
        if cmd == 109: on_log(body[3:])
        elif cmd == 101: on_status(body)
        elif cmd == 11: say(f"ОТВЕТ 11: код {body[3]} ({ {0:'ок',1:'параметр',2:'не выполнено',4:'нужен ПИН'}.get(body[3], '?')})")
        elif cmd == 102: say(f"ПИН {'принят' if body[3] else 'отвергнут'}")
        else: say(f"кадр {cmd} ({n} Б)")

h = wintypes.HANDLE(-1).value
while time.monotonic() - t0 < 10:
    h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
    if h != wintypes.HANDLE(-1).value: break
    time.sleep(0.05)
if h == wintypes.HANDLE(-1).value:
    raise SystemExit(f"COM5 не открылся, winerror={ctypes.get_last_error()}")
k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
n = wintypes.DWORD(); tmp = ctypes.create_string_buffer(4096)
import os
pin = sldn(102, os.environ.get('DOMOVOI_PIN', '4321').encode('ascii')[:5])
try:
    # После перезагрузки USB-сессия не авторизована: 108 без ПИН отвергается кодом 4.
    k.WriteFile(h, pin, len(pin), ctypes.byref(n), None); time.sleep(0.2)
    fr = sldn(108, bytes([bits])); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
    say(f"[host] ПИН отправлен, журнал биты {bits} включён")
    end = t0 + seconds; ping = sldn(101); next_ping = time.monotonic() + 1
    while time.monotonic() < end:
        ok = k.ReadFile(h, tmp, 4096, ctypes.byref(n), None)
        if ok and n.value: feed(tmp.raw[:n.value])
        if not ok:
            say(f"[host] чтение сорвалось winerror={ctypes.get_last_error()}, переоткрываю")
            k.CloseHandle(h); h = wintypes.HANDLE(-1).value
            while time.monotonic() < end and h == wintypes.HANDLE(-1).value:
                time.sleep(0.2); h = k.CreateFileW(r'\\.\COM5', 0xC0000000, 0, None, 3, 0, None)
            if h == wintypes.HANDLE(-1).value: break
            k.SetCommTimeouts(h, ctypes.byref(T(20, 0, 50, 0, 2000)))
            k.WriteFile(h, pin, len(pin), ctypes.byref(n), None); time.sleep(0.2)
            k.WriteFile(h, fr, len(fr), ctypes.byref(n), None); say("[host] порт переоткрыт, ПИН и журнал отправлены снова")
            continue
        if time.monotonic() >= next_ping:
            k.WriteFile(h, ping, len(ping), ctypes.byref(n), None); next_ping = time.monotonic() + ping_every
finally:
    if h != wintypes.HANDLE(-1).value:
        fr = sldn(108, b'\x00'); k.WriteFile(h, fr, len(fr), ctypes.byref(n), None)
        stop = time.monotonic() + 2
        while time.monotonic() < stop:
            if k.ReadFile(h, tmp, 4096, ctypes.byref(n), None) and n.value: feed(tmp.raw[:n.value])
        k.CloseHandle(h)
    say("[host] журнал выключен, порт закрыт")
    f.close(); raw.close()
