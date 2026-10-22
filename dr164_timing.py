"""Замеры DR164 перед командным режимом через сокет B (камера и мост на одной линии 485).
Запуск: py -3.11 -I dr164_timing.py <этап> [COM14]
этапы: m1 (A выключен постоянно: NETP сервером, B клиентом; переподключение B), m2 (когда мост
отвечает +ERR на байты камеры), m3 (ритм камеры, мусор в буфере моста).
Возврат моста — dr164_lab.py restore (снимок snap_before.json в папке логов дня).
"""
import os, re, socket, sys, time, random
import serial

sys.stdout.reconfigure(encoding='utf-8')
LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Логи', time.strftime('%Y-%m-%d') + '_dr164_опыты')
os.makedirs(LOGDIR, exist_ok=True)
HOST = '192.168.0.102'
PB = 15012          # сокет B
PA = 15013          # сокет A сервером на самом мосте: наружу не подключается
BYTE = 10 / 115200  # время байта на линии

stage = sys.argv[1]
com = sys.argv[2] if len(sys.argv) > 2 else 'COM14'
log = open(os.path.join(LOGDIR, time.strftime('%H%M%S_') + stage + '.txt'), 'w', encoding='utf-8')
pc = time.perf_counter
T0 = pc()


def out(*a):
    s = '%8.3f ' % (pc() - T0) + ' '.join(str(x) for x in a)
    print(s)
    log.write(s + '\n')
    log.flush()


ser = serial.Serial(com, 115200, timeout=0.005)


def tx(data):
    """Передать и вернуть оценку конца передачи на линии."""
    # Без ser.flush(): у CP210x он держит ~1 с. Конец — по времени байтов на линии.
    t = pc()
    ser.write(data)
    end = t + len(data) * 10 / ser.baudrate
    while pc() < end - 0.0005:
        pass
    return max(pc(), end)


def listen_for(sec, quiet=None):
    """Принимать sec секунд (или до тишины quiet после первого байта): [(время, байты)].
    Байт ждёт драйвер (read(1) с тайм-аутом 5 мс): частый опрос in_waiting тормозит запись CP210x."""
    got = []
    t = pc()
    while pc() - t < sec:
        if quiet and got and pc() - got[-1][0] >= quiet:
            break
        b = ser.read(1)
        if b:
            tb = pc()
            k = ser.in_waiting
            got.append((tb, b + (ser.read(k) if k else b'')))
    return got


def joined(got):
    return b''.join(b for _, b in got)


def at(cmd, show=True):
    time.sleep(0.05)
    ser.reset_input_buffer()
    tx(cmd.encode() + b'\r')
    got = listen_for(3.0, quiet=0.05)
    r = joined(got)
    if show:
        out('>', cmd, '<', r)
    return r


def val(r):
    m = re.search(rb'\+ok=?(.*?)\r', r)
    return m.group(1).decode(errors='replace') if m else None


def server(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, port))
    s.listen(4)
    s.setblocking(False)
    return s


def try_accept(s, sec):
    t = pc()
    while pc() - t < sec:
        try:
            c, peer = s.accept()
            c.setblocking(True)
            c.settimeout(10)
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return c, peer, pc() - t
        except BlockingIOError:
            time.sleep(0.02)
    return None, None, sec


def rcvb(n=1000):
    r = at('AT+RCVB=%d,0' % n, show=False)
    m = re.search(rb'\+ok=(\d+)', r)
    return r[m.end() + 1:m.end() + 1 + int(m.group(1))] if m and int(m.group(1)) else b''


def sndb(c, body):
    time.sleep(0.05)
    ser.reset_input_buffer()
    tx(b'AT+SNDB=%d\r' % len(body))
    p = joined(listen_for(1.0, quiet=0.05))
    if b'>' not in p:
        return 'нет приглашения: %r' % p
    time.sleep(0.05)
    tx(body)
    r = joined(listen_for(3.0, quiet=0.05))
    srv = b''
    if c:
        while len(srv) < len(body):
            srv += c.recv(len(body) - len(srv))
    return 'приглашение есть, ответ %r, сервер принял %d Б' % (r, len(srv))


def stats(name, xs):
    xs = sorted(xs)
    if not xs:
        out('  %s: ответов нет' % name)
        return
    ms = [x * 1000 for x in xs]
    out('  %s: n=%d мин %.1f мед %.1f макс %.1f мс' % (name, len(ms), ms[0], ms[len(ms) // 2], ms[-1]))


def first_reply(data, wait=0.5):
    """Задержка первого байта ответа от конца передачи и сам ответ."""
    time.sleep(0.15)
    ser.reset_input_buffer()
    end = tx(data)
    got = listen_for(wait, quiet=0.1)
    if not got:
        return None, b''
    return got[0][0] - end, joined(got)


try:
    if stage == 'm1':
        import subprocess

        def a_conns():
            # Подключения моста к 15010/15011 (порт 15010 занят стендовым «Нависетом»)
            t = subprocess.run(['netstat', '-an'], capture_output=True, text=True).stdout
            return {l.split()[2] for l in t.splitlines()
                    if '192.168.0.105:' in l and (':15010 ' in l or ':15011 ' in l)}
        sa, sb = server(PA), server(PB)
        pre = '' if val(at('USRAT+TMODE')) == 'cmd' else 'USR'
        for c in ('E=off', 'EVENT=off', 'TCPTO=0',
                  'NETP=TCP,Server,%d,%s' % (PA, HOST), 'SOCKB=TCP,%d,%s' % (PB, HOST), 'TMODE=cmd'):
            at(pre + 'AT+' + c)
        def drain(s):
            while True:
                x, _, _ = try_accept(s, 0.05)
                if not x:
                    return
                x.close()
        for rnd in (1, 2):
            out('=== перезапуск моста', rnd, '(AT+Z)')
            drain(sb)
            at(('USR' if rnd == 1 and pre else '') + 'AT+Z')
            t = pc()
            # Мост отвечает на Z сразу и грузится ~8 с; подключения до загрузки им уже оборваны
            ready = None
            while pc() - t < 30 and ready is None:
                time.sleep(0.2)
                ready = None if b'+ok' not in at('AT+TCPLKB', show=False) else pc() - t
            drain(sb)
            while pc() - t < 40 and val(at('AT+TCPLKB', show=False)) != 'on':
                time.sleep(0.2)
            c, peer, dt = try_accept(sb, 5)
            if not c:
                drain(sb)
                c, peer, dt = try_accept(sb, 20)
            out('мост ответил через %.1f с после Z, B на связи через %.1f с' % (ready or -1, pc() - t) if c else 'B НЕ подключился', peer)
            if rnd == 1:
                before = a_conns()
            time.sleep(5)
            x, p, _ = try_accept(sa, 0.1)
            out('  подключение к 15013:', p if x else 'нет')
            out('  новые подключения моста к 15010/15011:', sorted(a_conns() - before) or 'нет')
            before = a_conns()
            at('AT+E=off')
            for q in ('TMODE', 'NETP', 'SOCKB', 'TCPLK', 'TCPLKB', 'TCPDIS', 'TCPDISB'):
                at('AT+' + q)
            if c:
                for p in (b'0123456789ABCDEF', b'abcdefghijklmnopqrstuvw', b'XYZxyz012345'):
                    c.sendall(p)
                    time.sleep(0.1)
                    d = rcvb()
                    out('  B принял', 'ГОДНО' if d == p else 'ИСПОРЧЕНО %r' % d)
                out('  SNDB:', sndb(c, b'U' * 16))
                c.close()
                c = None
        out('=== сервер закрыл B: когда мост переподключится')
        t = pc()
        c, peer, dt = try_accept(sb, 60)
        out('  B снова подключился через %.1f с' % (pc() - t) if c else '  B не переподключился за 60 с')
        out('  TCPLKB', at('AT+TCPLKB', show=False))
        out('=== сервер выключен: что отвечает SNDB')
        if c:
            c.close()
        sb.close()
        time.sleep(1.5)
        out('  TCPLKB', at('AT+TCPLKB', show=False))
        out('  SNDB при выключенном сервере:', sndb(None, b'U' * 16))
        out('  RCVB при выключенном сервере:', at('AT+RCVB=100,0', show=False))
        sb = server(PB)
        t = pc()
        c, peer, dt = try_accept(sb, 60)
        out('  сервер снова включён: B подключился через %.1f с' % (pc() - t) if c else '  B не подключился за 60 с')
        out('  SMEM', at('AT+SMEM', show=False))
        if c:
            c.close()

    elif stage == 'm2':
        at('AT+E=off')
        SYNC = bytes.fromhex('AA0D00000000')
        out('=== a. обычная команда и SYNC камеры: задержка ответа от конца передачи')
        for name, data in (('AT+TMODE', b'AT+TMODE\r'), ('SYNC AA0D', SYNC), ('ACK AA0E0D', bytes.fromhex('AA0E0D000000'))):
            ds = []
            for _ in range(20):
                d, r = first_reply(data)
                if d is not None:
                    ds.append(d)
            stats(name, ds)
            out('    ответ', r)
        out('=== b. 0D в начале / в конце сплошного блока 512 Б')
        rnd = random.Random(1)
        body = bytes(rnd.choice([x for x in range(256) if x != 0x0D]) for _ in range(512))
        for name, data in (('0D в начале', b'\x0d' + body[1:]), ('0D в конце', body[:-1] + b'\x0d'),
                           ('0D в середине', body[:256] + b'\x0d' + body[257:]), ('без 0D', body)):
            ds = []
            rs = []
            for _ in range(10):
                d, r = first_reply(data, 0.6)
                rs.append(r)
                if d is not None:
                    ds.append(d)
            stats(name, ds)
            out('    ответы', set(rs))
        out('=== c. разрыв внутри посылки: AA 0D | пауза g | 00 00 00 00')
        for g in (0.001, 0.002, 0.003, 0.005, 0.008, 0.010, 0.012, 0.015, 0.020, 0.030, 0.050):
            hits = 0
            total = []
            for _ in range(5):
                time.sleep(0.15)
                ser.reset_input_buffer()
                end = tx(b'\xaa\x0d')
                gap = listen_for(g)
                tx(b'\x00\x00\x00\x00')
                rest = listen_for(0.3, quiet=0.1)
                if gap:
                    hits += 1
                total.append(joined(gap) + b'|' + joined(rest))
            out('  пауза %4.0f мс: ответ в паузе %d из 5; всё:' % (g * 1000, hits), set(total))
        out('=== d. мусор без 0D в буфере моста, потом команда')
        for w in (0.05, 0.2, 0.5, 1.0, 3.0):
            time.sleep(0.2)
            ser.reset_input_buffer()
            tx(b'\xaa\x04\x01\x00\x00\x00')
            time.sleep(w)
            out('  пауза %.2f с, затем AT+TMODE:' % w, at('AT+TMODE', show=False))
        time.sleep(0.2)
        tx(b'\xaa\x04\x01\x00\x00\x00')
        time.sleep(0.2)
        out('  мусор, затем одиночный CR как чистка:', first_reply(b'\r')[1], '-> затем', at('AT+TMODE', show=False))

    elif stage == 'm3':
        ERR = b'+ERR=-1\r\n\r\n'
        at('AT+E=off')
        rnd = random.Random(2)
        nod = [x for x in range(256) if x != 0x0D]
        out('=== порция моста: блоки без 0D разной длины (ответ позже конца = по тишине 20 мс)')
        for n in (100, 300, 511, 512, 513, 600, 1024):
            body = bytes(rnd.choice(nod) for _ in range(n))
            res = []
            for _ in range(3):
                d, r = first_reply(body, 0.3)
                res.append(('%.1f' % (d * 1000) if d is not None else '-', r))
                first_reply(b'\r', 0.1)  # чистка буфера моста
            out('  %4d Б:' % n, res)
        for lc, gm in ((0.060, 0.004), (0.060, 0.040), (0.022, 0.004)):
            out('=== ритм камеры: пакет через %.0f мс после запроса, следующий запрос через %.0f мс после пакета' % (lc * 1000, gm * 1000))
            seen = []
            for pid in range(25):
                req = bytes([0xAA, 0x0E, 0, 0, pid & 0xFF, pid >> 8])
                e1 = tx(req)
                g1 = listen_for(lc)
                pkt = bytes(rnd.randrange(256) for _ in range(512 if pid < 24 else 300))
                e2 = tx(pkt)
                g2 = listen_for(gm)
                for g, e, where in ((g1, e1, 'после запроса'), (g2, e2, 'после пакета')):
                    if g:
                        seen.append((pid, where, round((g[0][0] - e) * 1000, 1), joined(g)))
            tail = listen_for(0.3, quiet=0.1)
            good = sum(1 for x in seen if x[3] == ERR)
            out('  ответов моста в паузах: %d, из них чистых %d' % (len(seen), good))
            for x in seen:
                if x[3] != ERR or x[1] == 'после запроса' or x[0] in (0, 23, 24):
                    out('   ', x)
            out('  задержки после пакета, мс:', sorted({x[2] for x in seen if x[1] == 'после пакета'}))
            out('  после последнего пакета:', joined(tail))
            out('  команда после серии:', at('AT+TMODE', show=False))
            out('  CR-чистка:', first_reply(b'\r', 0.1)[1], 'и снова:', at('AT+TMODE', show=False))
    elif stage == 'm4':
        # Пороги буфера команд моста: «длинная строка» по тишине и «буфер полон» сразу
        at('AT+E=off')
        rnd = random.Random(4)
        nod = [x for x in range(256) if x not in (0x0D, 0x0A)]

        def probe(body, label):
            first_reply(b'\r', 0.1)  # чистый буфер моста
            d, r = first_reply(body, 0.3)
            first_reply(b'\r', 0.1)
            out('  %-34s %s %r' % (label, ('%.1f мс' % (d * 1000)) if d is not None else 'молчит', r))
            return d
        out('=== длина без 0D/0A: где начинается ответ по тишине (~21 мс)')
        lo, hi = 513, 600
        while hi - lo > 1:
            mid = (lo + hi) // 2
            d = probe(bytes(rnd.choice(nod) for _ in range(mid)), '%d Б' % mid)
            if d is None:
                lo = mid
            else:
                hi = mid
        out('  -> молчит до %d Б, отвечает с %d Б' % (lo, hi))
        out('=== где буфер полон (ответ сразу после конца, ~2 мс)')
        for n in (900, 1000, 1020, 1023, 1024, 1025, 1030, 1040, 1100, 1536, 2048):
            probe(bytes(rnd.choice(nod) for _ in range(n)), '%d Б' % n)
        out('=== 0A как конец строки?')
        probe(bytes.fromhex('AA0E00000A00'), 'запрос с 0A')
        probe(bytes.fromhex('AA0E00000D00'), 'запрос с 0D')
        probe(bytes(rnd.choice(nod) for _ in range(100)) + b'\n', '100 Б + 0A')
        out('=== накопление: запрос 6 Б + пакет 512 Б без 0D, пауза 60 мс, снова')
        first_reply(b'\r', 0.1)
        for k in range(4):
            e = tx(bytes.fromhex('AA0E00000100'))
            g1 = listen_for(0.06)
            e2 = tx(bytes(rnd.choice(nod) for _ in range(512)))
            g2 = listen_for(0.06)
            out('  круг %d: после запроса %r, после пакета %s' % (
                k, joined(g1), ('%.1f мс %r' % ((g2[0][0] - e2) * 1000, joined(g2))) if g2 else 'тишина'))
        first_reply(b'\r', 0.1)
    elif stage == 'm5':
        # Сетка: один 0D в позиции p блока n Б (прочие байты без 0D/0A), буфер моста чистый.
        at('AT+E=off')
        rnd = random.Random(5)
        nod = [x for x in range(256) if x not in (0x0D, 0x0A)]

        def block(n, p):
            b = bytearray(rnd.choice(nod) for _ in range(n))
            if p is not None:
                b[p] = 0x0D
            return bytes(b)

        def clean():
            first_reply(b'\r', 0.1)
            time.sleep(0.1)

        out('=== A. один блок, 0D в позиции p (задержка от конца блока)')
        for n, p in ((6, 1), (100, 50), (300, 50), (511, 50), (512, 50), (512, 511), (513, 50),
                     (600, 50), (600, 580), (1024, 50), (1024, 1000), (1100, 50)):
            res = []
            for _ in range(3):
                clean()
                d, r = first_reply(block(n, p), 0.3)
                res.append('%s %r' % (('%.1f' % (d * 1000)) if d is not None else '-', r))
            out('  n=%4d p=%4d:' % (n, p), ' | '.join(res))
        out('=== B. сначала мусор G Б без 0D/0A, тишина 100 мс, затем 512 Б с 0D в позиции 50')
        for g in (6, 300, 506, 512, 518, 1000):
            res = []
            for _ in range(3):
                clean()
                tx(block(g, None))
                time.sleep(0.1)
                d, r = first_reply(block(512, 50), 0.3)
                res.append('%s %r' % (('%.1f' % (d * 1000)) if d is not None else '-', r))
            out('  G=%4d:' % g, ' | '.join(res))
        clean()
    elif stage == 'm6':
        # Мост на 460800, камера и линия на 115200: слышит ли мост 0D/0A в байтах камеры
        FAST = int(sys.argv[3]) if len(sys.argv) > 3 else 460800
        ERR = b'+ERR=-1\r\n\r\n'
        rnd = random.Random(6)
        rich = [0x0D, 0x0A] * 20 + list(range(256))  # байты камеры, богатые 0D/0A

        def rich_block(n):
            return bytes(rnd.choice(rich) for _ in range(n))

        def camera_traffic(label, rounds):
            """Трафик камеры на 115200 с паузами; что мост отправил в паузах."""
            ser.baudrate = 115200
            heard = []
            for i in range(rounds):
                for blk in (bytes.fromhex('AA0D00000000'), bytes([0xAA, 0x0E, 0, 0, i & 0xFF, 0]),
                            rich_block(90), rich_block(512)):
                    tx(blk)
                    g = listen_for(0.04)
                    if g:
                        heard.append(joined(g))
            out('  %s: %d посылок, мост ответил в паузах %d раз %r' % (label, rounds * 4, len(heard), heard[:5]))
            return len(heard)

        out('=== контроль: мост на 115200 — отвечает на байты камеры')
        ser.baudrate = 115200
        at('AT+E=off')
        first_reply(b'\r', 0.1)
        camera_traffic('мост 115200', 5)
        ser.baudrate = 115200
        first_reply(b'\r', 0.1)
        out('=== мост на %d' % FAST)
        at('AT+UART=%d,8,1,NONE,NFC' % FAST)
        at('AT+Z')
        ser.baudrate = FAST
        time.sleep(6)
        for _ in range(30):
            if val(at('AT+TMODE', show=False)) == 'cmd':
                break
            time.sleep(0.3)
        at('AT+UART')
        at('AT+E=off')
        n = camera_traffic('мост %d' % FAST, 25)
        ser.baudrate = FAST
        out('  после трафика: CR ->', first_reply(b'\r', 0.2)[1], ', AT+TMODE ->', at('AT+TMODE', show=False))
        out('=== что мост «услышал» из трафика камеры (эхо включено)')
        at('AT+E=on')
        first_reply(b'\r', 0.2)
        ser.baudrate = 115200
        tx(rich_block(90))
        time.sleep(0.05)
        tx(rich_block(512))
        time.sleep(0.05)
        ser.baudrate = FAST
        heard = first_reply(b'\r', 0.3)[1]
        out('  эхо строки (%d Б): %s' % (len(heard), heard[:200].hex(' ')))
        out('  байты в эхо:', sorted(set(heard[:-len(ERR)] if heard.endswith(ERR) else heard)))
        at('AT+E=off')
        first_reply(b'\r', 0.2)
        out('=== объём: 20 КБ трафика камеры на 115200 подряд, затем команда')
        ser.baudrate = 115200
        for _ in range(40):
            tx(rich_block(512))
        listen_for(0.1)
        ser.baudrate = FAST
        out('  CR ->', first_reply(b'\r', 0.3)[1], ', AT+TMODE ->', at('AT+TMODE', show=False))
        out('=== сокет B на %d: 100 приёмов и 25 отправок' % FAST)
        sb = server(PB)
        at('AT+TCPDISB=off,0')
        time.sleep(0.5)
        at('AT+TCPDISB=on,0')
        c, peer, dt = try_accept(sb, 30)
        out('  B подключился', peer)
        bad = sbad = 0
        if c:
            for i in range(100):
                k = rnd.randint(1, 1000)
                body = bytes(rnd.randrange(256) for _ in range(k))
                c.sendall(body)
                time.sleep(0.03)
                got = b''
                t = pc()
                while len(got) < k and pc() - t < 3:
                    d = rcvb(1000)
                    got += d
                if got != body:
                    bad += 1
                if i % 4 == 0:
                    r = sndb(c, bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 1000))))
                    if 'принял 0' in r or 'нет приглашения' in r:
                        sbad += 1
            c.close()
        out('  ошибок приёма %d из 100, отправки %d из 25' % (bad, sbad))
        sb.close()
        out('=== возврат 115200')
        at('AT+UART=115200,8,1,NONE,NFC')
        at('AT+Z')
        ser.baudrate = 115200
        time.sleep(6)
        for _ in range(30):
            if val(at('AT+TMODE', show=False)) == 'cmd':
                break
            time.sleep(0.3)
        at('AT+UART')
    elif stage == 'm7':
        # Откуда редкие ответы моста на быстрой скорости: тип посылки, задержка, повторяемость
        rnd = random.Random(7)
        rich = [0x0D, 0x0A] * 20 + list(range(256))
        fixed = {'SYNC': bytes.fromhex('AA0D00000000'), 'ACK0D': bytes.fromhex('AA0E0D000000'),
                 'ACK': bytes.fromhex('AA0E00000500'),
                 'R90': bytes(rnd.choice(rich) for _ in range(90)),
                 'R512': bytes(rnd.choice(rich) for _ in range(512)),
                 'Z512': bytes(512), 'F512': b'\xff' * 512}

        def setbaud(b):
            at('AT+UART=%d,8,1,NONE,NFC' % b)
            at('AT+Z')
            ser.baudrate = b
            time.sleep(6)
            for _ in range(30):
                if val(at('AT+TMODE', show=False)) == 'cmd':
                    break
                time.sleep(0.3)
            at('AT+E=off')

        for fast in (460800, 921600):
            ser.baudrate = 115200
            setbaud(fast)
            stat = {}
            ser.baudrate = 115200
            for rep in range(40):
                for name, blk in list(fixed.items()) + [('RND512', bytes(rnd.choice(rich) for _ in range(512)))]:
                    e = tx(blk)
                    g = listen_for(0.05)
                    s = stat.setdefault(name, [0, 0, []])
                    s[0] += 1
                    if g:
                        s[1] += 1
                        s[2].append(round((g[0][0] - e) * 1000, 1))
            out('=== мост %d, линия 115200, по 40 посылок каждого вида:' % fast)
            for name, (n, k, ds) in stat.items():
                out('  %-7s ответов %2d из %d, задержки мс %s' % (name, k, n, sorted(ds)[:8]))
            ser.baudrate = fast
            first_reply(b'\r', 0.2)
        ser.baudrate = 921600
        setbaud(115200)
        at('AT+UART')
    elif stage == 'm8':
        # Мост 460800 + UARTTM=1000 на время съёмки: ноль ответов в трафике камеры?
        rnd = random.Random(8)
        rich = [0x0D, 0x0A] * 20 + list(range(256))

        def find_baud():
            for b in (460800, 115200, 921600, 230400):
                ser.baudrate = b
                first_reply(b'\r', 0.1)
                if val(at('AT+TMODE', show=False)) == 'cmd':
                    out('мост отвечает на', b)
                    return b
            raise SystemExit('мост не найден')

        def setbaud(b):
            at('AT+UART=%d,8,1,NONE,NFC' % b)
            at('AT+Z')
            ser.baudrate = b
            time.sleep(6)
            for _ in range(30):
                if val(at('AT+TMODE', show=False)) == 'cmd':
                    break
                time.sleep(0.3)
            at('AT+E=off')

        def cmd_delay(c):
            first_reply(b'\r', 1.5)
            d, r = first_reply(c.encode() + b'\r', 2.5)
            return '%s -> %.0f мс %r' % (c, (d or -1) * 1000, r)

        if find_baud() != 460800:
            setbaud(460800)
        at('AT+UART')
        out('=== UARTTM действует сразу?')
        out(' ', cmd_delay('AT+UARTTM'))
        at('AT+UARTTM=1000')
        out(' ', cmd_delay('AT+TMODE'))
        out(' ', cmd_delay('AT+UARTTM'))
        out('=== трафик камеры 115200 при UARTTM=1000: по 40 посылок 8 видов, паузы 50 мс')
        fixed = {'SYNC': bytes.fromhex('AA0D00000000'), 'ACK0D': bytes.fromhex('AA0E0D000000'),
                 'ACK': bytes.fromhex('AA0E00000500'), 'R90': bytes(rnd.choice(rich) for _ in range(90)),
                 'R512': bytes(rnd.choice(rich) for _ in range(512)), 'Z512': bytes(512),
                 'F512': b'\xff' * 512}
        ser.baudrate = 115200
        k = 0
        n = 0
        ds = []
        for rep in range(40):
            for name, blk in list(fixed.items()) + [('RND512', bytes(rnd.randrange(256) for _ in range(512)))]:
                e = tx(blk)
                g = listen_for(0.05)
                n += 1
                if g:
                    k += 1
                    ds.append((name, round((g[0][0] - e) * 1000, 1)))
        out('  ответов в паузах: %d из %d %s' % (k, n, ds[:10]))
        g = listen_for(2.0)
        out('  через 2 с тишины пришло:', [(round((t - g[0][0]) * 1000), b) for t, b in g] if g else 'ничего')
        ser.baudrate = 460800
        out('  CR ->', first_reply(b'\r', 1.5)[1])
        at('AT+UARTTM=20')
        out(' ', cmd_delay('AT+TMODE'))
        at('AT+UARTTM')
        out('=== возврат 115200')
        setbaud(115200)
        at('AT+UART')
        at('AT+UARTTM')
finally:
    ser.close()
    log.close()
