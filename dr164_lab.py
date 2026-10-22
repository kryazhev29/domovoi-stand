"""Опыты с DR164 через USB-485 (отчёт Codex docs/history/DR164_ANALYSIS_20261020.md §3).
Запуск: py -3.11 -I dr164_lab.py <этап> [COM14]
этапы: snap (снимок до опытов), setup (командный режим, порт 15011), e1, e1t, e2, e3, e3q,
e4 (сокет B), e4b (сокет B: очередь, частями, SNDB, 200 обменов), noise (байты камеры), restore
Мост на USB-485 (CP210x, COM14), плата и камера от линии отключены; сервер — эта машина 192.168.0.102.
"""
import json, os, re, socket, sys, time
import serial

sys.stdout.reconfigure(encoding='utf-8')
LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Логи', time.strftime('%Y-%m-%d') + '_dr164_опыты')
os.makedirs(LOGDIR, exist_ok=True)
SNAP = os.path.join(LOGDIR, 'snap_before.json')
HOST, PORT = '192.168.0.102', 15011
P1 = b'0123456789ABCDEF'
P2 = b'abcdefghijklmnopqrstuvw'
P3 = b'XYZxyz012345'

stage = sys.argv[1]
com = sys.argv[2] if len(sys.argv) > 2 else 'COM14'
log = open(os.path.join(LOGDIR, time.strftime('%H%M%S_') + stage + '.txt'), 'w', encoding='utf-8')
T0 = time.time()


def out(*a):
    s = '%8.3f ' % (time.time() - T0) + ' '.join(str(x) for x in a)
    print(s)
    log.write(s + '\n')
    log.flush()


ser = serial.Serial(com, 115200, timeout=0.01)


def rx_quiet(total=3.0, quiet=0.05, need=None):
    """Читать до тишины quiet после последнего байта (или до total)."""
    buf = b''
    t = time.time()
    last = None
    while time.time() - t < total:
        b = ser.read(512)
        if b:
            buf += b
            last = time.time()
        elif last and time.time() - last >= quiet and (need is None or need(buf)):
            break
    return buf


def at(cmd, total=3.0, show=True):
    time.sleep(0.05)
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b'\r')
    r = rx_quiet(total, need=lambda b: b'+ok' in b or b'+ERR' in b)
    if show:
        out('>', cmd, '<', r)
    return r


def val(r):
    m = re.search(rb'\+ok=?(.*?)\r', r)
    return m.group(1).decode(errors='replace') if m else None


def recv(n, t=0, total=4.0):
    """AT+RECV=n,t -> (байты данных, сырой ответ)."""
    time.sleep(0.05)
    ser.reset_input_buffer()
    ser.write(b'AT+RECV=%d,%d\r' % (n, t))

    def done(b):
        m = re.search(rb'\+ok=(\d+)', b)
        if not m:
            return b'+ERR' in b
        k = int(m.group(1))
        return k == 0 or len(b) >= m.end() + 1 + k
    r = rx_quiet(total + t, need=done)
    m = re.search(rb'\+ok=(\d+)', r)
    data = b''
    if m and int(m.group(1)) > 0:
        k = int(m.group(1))
        data = r[m.end() + 1:m.end() + 1 + k]
    out('> RECV=%d,%d < %d Б:' % (n, t, len(data)), r.hex(' '))
    return data, r


def check(name, got, want):
    ok = got == want
    out('   %s %s: %r' % ('ГОДНО' if ok else 'ИСПОРЧЕНО', name, got))
    if not ok:
        out('   ждали', want.hex(' '))
        out('   пришло', got.hex(' '))
    return ok


# ---- сервер ----
lsock = None
conn = None


def listen():
    global lsock
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((HOST, PORT))
    lsock.listen(4)
    at('AT+E=off')


def drop_pending():
    lsock.setblocking(False)
    while True:
        try:
            c, _ = lsock.accept()
            c.close()
        except (BlockingIOError, OSError):
            break
    lsock.setblocking(True)


def new_tcp():
    """Новое соединение моста: TCPDIS off -> закрыть всё -> on -> accept."""
    global conn
    at('AT+TCPDIS=off,0')
    if conn:
        conn.close()
        conn = None
    time.sleep(0.3)
    drop_pending()
    at('AT+TCPDIS=on,0')
    lsock.settimeout(40)
    conn, peer = lsock.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(10)
    out('TCP принят от', peer)
    for _ in range(50):
        if val(at('AT+TCPLK', show=False)) == 'on':
            break
        time.sleep(0.2)
    out('TCPLK on')


def srv_send(p):
    conn.sendall(p)
    out('сервер ->', len(p), 'Б')


def srv_read(n):
    b = b''
    while len(b) < n:
        b += conn.recv(n - len(b))
    return b


QUERIES = ['VER', 'WEBVER', 'SN', 'TMODE', 'NETP', 'SOCKB', 'TCPDIS', 'TCPDISB', 'EVENT',
           'TCPTO', 'UART', 'UARTTM', 'UARTBUF', 'UDPLCPT', 'WSSSID', 'WSLK', 'WANN', 'CMDPW',
           'REGEN', 'HEARTEN', 'MODBUSEN', 'SMEM']


def snap(prefix):
    d = {}
    for q in QUERIES:
        d[q] = val(at(prefix + 'AT+' + q))
    return d


try:
    if stage == 'snap':
        d = snap('USR')
        if not os.path.exists(SNAP):
            json.dump(d, open(SNAP, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            out('снимок сохранён', SNAP)
        out(json.dumps(d, ensure_ascii=False))

    elif stage == 'setup':
        assert os.path.exists(SNAP), 'сначала snap'
        listen()
        for c in ('E=off', 'EVENT=off', 'TCPTO=0', 'TCPDISB=off,0', 'TCPDIS=off,0',
                  'NETP=TCP,CLIENT,%d,%s' % (PORT, HOST), 'TMODE=cmd'):
            at('USRAT+' + c)
        at('USRAT+Z')
        time.sleep(8)
        for _ in range(100):
            r = at('AT+TCPLK', show=False)
            if r:
                break
            time.sleep(0.2)
        out('после Z:', r)
        snap('')

    elif stage in ('e1', 'e1t'):
        listen()
        t = 1 if stage == 'e1t' else 0
        good = 0
        for run in range(1, 4):
            out('=== прогон', run, 'RECV ...,%d' % t)
            new_tcp()
            for name, p in (('P1', P1), ('P2', P2), ('P3', P3)):
                if t:
                    import threading
                    threading.Timer(0.3, srv_send, (p,)).start()
                else:
                    srv_send(p)
                    time.sleep(0.1)
                got = b''
                for _ in range(10):
                    d, _ = recv(512, t)
                    got += d
                    if len(got) >= len(p):
                        break
                    if not d:
                        time.sleep(0.3)
                good += check(name, got, p)
        out('ИТОГ', stage, 'годных', good, 'из 9')

    elif stage == 'e2':
        listen()
        U = b'U' * 16
        M = b'M' * 16

        def send_u(body):
            r = at('AT+SEND=%d' % len(body))
            time.sleep(0.05)
            ser.write(body)
            r = rx_quiet(3.0, need=lambda b: b'+ok' in b or b'+ERR' in b)
            out('  данные SEND <', r)
            out('  сервер принял', srv_read(len(body)))

        def take(p, name):
            time.sleep(0.1)
            d, _ = recv(512, 0)
            return check(name, d, p)
        res = {}
        for body in (U, M):
            for case in ('C', 'A', 'B', 'D'):
                out('=== случай', case, body[:1])
                new_tcp()
                srv_send(P1)
                take(P1, 'P1')
                if case == 'C':
                    srv_send(P2)
                    ok = take(P2, 'P2')
                elif case == 'A':
                    send_u(body)
                    srv_send(P2)
                    ok = take(P2, 'P2')
                elif case == 'B':
                    srv_send(P2)
                    time.sleep(0.1)
                    send_u(body)
                    ok = take(P2, 'P2')
                else:
                    srv_send(P2)
                    ok = take(P2, 'P2')
                    send_u(body)
                    srv_send(P3)
                    ok = take(P3, 'P3') and ok
                res['%s_%s' % (case, body[:1].decode())] = ok
        out('ИТОГ e2', res)

    elif stage == 'e3':
        listen()
        blk = bytes(range(64))
        new_tcp()
        srv_send(blk)
        time.sleep(0.1)
        got = b''
        for n in (16, 16, 32, 32):
            d, _ = recv(n, 0)
            got += d
        check('64 по частям', got, blk)
        new_tcp()
        srv_send(blk)
        time.sleep(0.1)
        d, _ = recv(64, 0)
        check('64 одним', d, blk)

    elif stage == 'e3q':
        listen()
        new_tcp()
        for p in (P1, P2, P3):
            srv_send(p)
            time.sleep(0.1)
        got = b''
        for _ in range(10):
            d, _ = recv(512, 0)
            if not d:
                break
            got += d
        check('очередь P1+P2+P3', got, P1 + P2 + P3)

    elif stage == 'e4':
        # Сокет B на отдельный порт 15012, A держим выключенным
        listen()
        lb = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lb.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lb.bind((HOST, 15012))
        lb.listen(4)
        at('AT+TCPDIS=off,0')
        at('AT+SOCKB=TCP,15012,%s' % HOST)
        at('AT+TCPDISB=on,0')
        lb.settimeout(40)
        try:
            cb, peer = lb.accept()
        except socket.timeout:
            out('B не подключился без Z — делаю Z')
            at('AT+Z')
            time.sleep(8)
            at('AT+E=off')
            at('AT+TCPDIS=off,0')
            cb, peer = lb.accept()
        cb.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        out('B принят от', peer)
        for _ in range(50):
            r = at('AT+TCPLKB', show=False)
            if val(r) == 'on':
                break
            time.sleep(0.2)
        out('TCPLKB', r)
        good = 0
        for name, p in (('P1', P1), ('P2', P2), ('P3', P3)):
            cb.sendall(p)
            out('сервер B ->', len(p))
            time.sleep(0.1)
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.write(b'AT+RCVB=512,0\r')
            r = rx_quiet(4.0, need=lambda b: b'+ok' in b or b'+ERR' in b)
            m = re.search(rb'\+ok=(\d+)', r)
            d = r[m.end() + 1:m.end() + 1 + int(m.group(1))] if m and int(m.group(1)) else b''
            out('> RCVB <', r.hex(' '))
            good += check(name, d, p)
        out('ИТОГ e4 годных', good, 'из 3')
        at('AT+TCPDISB=off,0')
        at('AT+SOCKB=NONE')
        cb.close()
        lb.close()

    elif stage == 'e4b':
        import random
        random.seed(20261020)
        listen()
        lb = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lb.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lb.bind((HOST, 15012))
        lb.listen(4)
        at('AT+SOCKB=TCP,15012,%s' % HOST)
        at('AT+Z')
        time.sleep(8)
        at('AT+E=off')
        at('AT+TCPDIS=off,0')
        lb.settimeout(40)
        cb, peer = lb.accept()
        cb.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        cb.settimeout(10)
        out('B принят от', peer, 'TCPLKB', at('AT+TCPLKB', show=False))

        def rcvb(n):
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.write(b'AT+RCVB=%d,0\r' % n)

            def done(b):
                m = re.search(rb'\+ok=(\d+)', b)
                if not m:
                    return b'+ERR' in b
                k = int(m.group(1))
                return k == 0 or len(b) >= m.end() + 1 + k
            r = rx_quiet(4.0, need=done)
            m = re.search(rb'\+ok=(\d+)', r)
            if not m:
                out('  RCVB без +ok:', r[:80])
                return b''
            k = int(m.group(1))
            return r[m.end() + 1:m.end() + 1 + k] if k else b''

        def take(want, n=1000):
            got = b''
            t = time.time()
            while len(got) < len(want) and time.time() - t < 3:
                d = rcvb(n)
                got += d
                if not d:
                    time.sleep(0.05)
            return got

        def sndb(body):
            time.sleep(0.05)
            ser.reset_input_buffer()
            ser.write(b'AT+SNDB=%d\r' % len(body))
            p = rx_quiet(1.0, need=lambda b: b'>' in b or b'+ERR' in b)
            if b'>' not in p:
                out('  SNDB без приглашения:', p)
                return False
            time.sleep(0.05)
            ser.write(body)
            r = rx_quiet(3.0, need=lambda b: b'+ok' in b or b'+ERR' in b)
            m = re.search(rb'\+ok=(\d+)', r)
            srv = b''
            while len(srv) < len(body):
                srv += cb.recv(len(body) - len(srv))
            ok = srv == body and m and int(m.group(1)) == len(body)
            if not ok:
                out('  SNDB плохо: подтверждение', r, 'сервер', len(srv))
            return ok

        res = {}
        # 1. очередь из трёх до чтения
        for p in (P1, P2, P3):
            cb.sendall(p)
            time.sleep(0.1)
        res['очередь'] = check('очередь', take(P1 + P2 + P3), P1 + P2 + P3)
        # 2. частичное чтение
        blk = bytes(range(64))
        cb.sendall(blk)
        time.sleep(0.1)
        got = rcvb(16) + rcvb(16) + rcvb(32)
        res['частями'] = check('частями', got, blk)
        # 3. SNDB и чтение после
        res['SNDB_приглашение'] = sndb(b'U' * 16)
        cb.sendall(P2)
        time.sleep(0.1)
        res['после_SNDB'] = check('после SNDB', take(P2), P2)
        # 4. 200 обменов
        bad = 0
        sbad = 0
        for i in range(200):
            n = random.choice((1, 2, 12, 16, 23, 48, 100, 255, 256, 511, 512, 513, 999, 1000)) if i % 3 else random.randint(1, 1000)
            body = bytes(random.choice((0x00, 0x0A, 0x0D, 0xFF, random.randint(0, 255))) for _ in range(n))
            cb.sendall(body)
            time.sleep(0.02 + n / 11520)
            got = take(body, random.choice((1000, 512, 256)))
            if got != body:
                bad += 1
                out('  обмен', i, 'длина', n, 'ПЛОХО: пришло', len(got))
            if i % 4 == 0:
                sn = random.randint(1, 1000)
                if not sndb(bytes(random.randint(0, 255) for _ in range(sn))):
                    sbad += 1
            if i % 25 == 0:
                out('  обмен', i, 'ошибок приёма', bad, 'отправки', sbad)
        res['200_приём_ошибок'] = bad
        res['50_отправка_ошибок'] = sbad
        out('ИТОГ e4b', json.dumps(res, ensure_ascii=False))
        out('SMEM', at('AT+SMEM', show=False))
        cb.close()
        lb.close()

    elif stage == 'noise':
        # Командный режим: отвечает ли мост на чужие байты в линии (кадры камеры C328)
        import random
        random.seed(328)
        if val(at('USRAT+TMODE')) != 'cmd':
            at('USRAT+TMODE=cmd')
            at('USRAT+Z')
            time.sleep(8)
        at('AT+E=off')
        frames = [('SYNC', bytes.fromhex('AA0D00000000')),
                  ('ACK', bytes.fromhex('AA0E0D000000')),
                  ('GET PICTURE', bytes.fromhex('AA0401000000')),
                  ('с CR в конце', bytes.fromhex('AA0A0100300D')),
                  ('пакет 512 случайных', bytes(random.randint(0, 255) for _ in range(512))),
                  ('пакет 512 c 0D 0A', bytes(random.choice((0x0D, 0x0A, 0x41, 0x54, 0x2B, 0xFF)) for _ in range(512))),
                  ('похоже на AT', b'xxAT+TMODE\r'),
                  ('+++', b'+++')]
        for name, f in frames:
            time.sleep(0.1)
            ser.reset_input_buffer()
            ser.write(f)
            r = rx_quiet(1.2, quiet=1.0)
            out('%-22s %4d Б -> мост ответил %d Б: %r' % (name, len(f), len(r), r[:80]))
            if r.startswith(b'a'):
                ser.write(b'\x1b')  # не входить во временный режим
                out('   после +++ пришло a, ждём 3 с без ответа')
                time.sleep(3.5)
                ser.reset_input_buffer()
        out('проверка, что мост жив:', at('AT+TMODE', show=False))

    elif stage == 'restore':
        d = json.load(open(SNAP, encoding='utf-8'))
        net = d['NETP']
        pre = '' if val(at('AT+TMODE')) == 'cmd' else 'USR'
        at(pre + 'AT+TCPDIS=on,0')
        at(pre + 'AT+NETP=' + net)
        at(pre + 'AT+SOCKB=' + d['SOCKB'])
        at(pre + 'AT+E=off')
        at(pre + 'AT+EVENT=' + d['EVENT'])
        at(pre + 'AT+TCPTO=' + d['TCPTO'])
        at(pre + 'AT+TMODE=' + d['TMODE'])
        at(pre + 'AT+Z')
        time.sleep(8)
        out('после восстановления:', json.dumps(snap('USR'), ensure_ascii=False))
finally:
    if conn:
        conn.close()
    if lsock:
        lsock.close()
    ser.close()
    log.close()
