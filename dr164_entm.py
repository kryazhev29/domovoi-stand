"""Временный прозрачный режим DR164 (AT+ENTM, выход +++/a) при сокете B: можно ли так заглушить мост на время съёмки.
Запуск: py -3.11 -I dr164_entm.py <скорость моста> [COM14]
Вопросы: уходят ли байты линии в сокет B; приходят ли данные B в линию; копятся ли они до возврата; рвётся ли TCP;
отвечает ли мост +ERR на байты с 0D/0A в прозрачном режиме. Сокет B на время опыта - на 192.168.0.102:15012,
в конце возвращается по снимку snap_before.json (папка Логи/<дата>_dr164_entm)."""
import json, os, re, select, socket, sys, time
import serial

sys.stdout.reconfigure(encoding='utf-8')
BAUD = int(sys.argv[1]); COM = sys.argv[2] if len(sys.argv) > 2 else 'COM14'
LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Логи', time.strftime('%Y-%m-%d') + '_dr164_entm')
os.makedirs(LOGDIR, exist_ok=True)
SNAP = os.path.join(LOGDIR, 'snap_before.json')
HOST, PORT = '192.168.0.102', 15012
log = open(os.path.join(LOGDIR, time.strftime('%H%M%S') + '_entm.txt'), 'w', encoding='utf-8')
pc = time.perf_counter; T0 = pc()


def out(*a):
    s = '%8.3f ' % (pc() - T0) + ' '.join(str(x) for x in a)
    print(s); log.write(s + '\n'); log.flush()


ser = serial.Serial(COM, BAUD, timeout=0.005)


def listen(sec, quiet=None):
    """Байты линии за sec секунд (или до тишины quiet после первого байта); ждёт драйвер, без частого опроса."""
    got = b''; t = pc(); last = None
    while pc() - t < sec:
        if quiet and last and pc() - last >= quiet:
            break
        b = ser.read(1)
        if b:
            k = ser.in_waiting
            got += b + (ser.read(k) if k else b''); last = pc()
    return got


def at(cmd, wait=1.0, show=True):
    time.sleep(0.05); ser.reset_input_buffer(); ser.write(cmd.encode() + b'\r')
    r = listen(wait, quiet=0.06)
    if show:
        out('>', cmd, '<', r)
    return r


def val(r):
    m = re.search(rb'\+ok=?(.*?)\r', r)
    return m.group(1).decode(errors='replace') if m else None


def srv_take(c, sec):
    """Что пришло на сервер (сокет B) за sec секунд."""
    got = b''; end = pc() + sec
    while pc() < end:
        r, _, _ = select.select([c], [], [], max(0.0, end - pc()))
        if not r:
            break
        d = c.recv(4096)
        if not d:
            out('  сервер: мост закрыл соединение'); break
        got += d
    return got


def leave_transparent():
    """Выход из временного прозрачного режима: тишина, +++, ждать 'a', ответить 'a', ждать +ok."""
    time.sleep(0.1); ser.reset_input_buffer()
    t = pc(); ser.write(b'+++')
    a = listen(1.0, quiet=0.05)
    ta = pc() - t
    if b'a' not in a:
        out('  +++: нет "a", пришло', a); return False
    ser.write(b'a')
    ok = listen(1.0, quiet=0.06)
    out('  +++ -> %r за %.0f мс, a -> %r за %.0f мс' % (a, ta * 1000, ok, (pc() - t - ta) * 1000))
    return b'+ok' in ok


lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
lsock.bind((HOST, PORT)); lsock.listen(4)
conn = None
snap = json.load(open(SNAP, encoding='utf-8'))
try:
    at('')
    at('AT+SOCKB=TCP,%d,%s' % (PORT, HOST))
    at('AT+Z', wait=0.3)
    time.sleep(8.5)
    for _ in range(60):  # мост сам скажет, что B на связи; подключения до перезапуска им уже оборваны
        at('', show=False)
        if val(at('AT+TCPLKB', show=False)) == 'on':
            break
        time.sleep(0.3)
    lsock.settimeout(0.3)
    try:
        while True:  # взять самое свежее подключение
            c2, p2 = lsock.accept()
            if conn:
                conn.close()
            conn, peer = c2, p2
    except socket.timeout:
        pass
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    out('B подключился', peer); at('AT+E=off'); out('TCPLKB', val(at('AT+TCPLKB', show=False)))
    srv_take(conn, 0.2)
    out('=== 1. командный режим: сервер шлёт P1, RCVB')
    conn.sendall(b'P1-server-data'); time.sleep(0.1)
    out('  RCVB:', at('AT+RCVB=512,0', show=False))
    rich = bytes([0x0D, 0x0A, 0x41, 0x54, 0x2B, 0xAA, 0x00, 0xFF] * 64)  # 512 Б с CR/LF и «AT+»
    results = []
    for cycle in range(1, int(os.environ.get("ENTM_CYCLES", "5")) + 1):
        out('=== цикл', cycle)
        t = pc(); r = at('AT+ENTM', show=False); out('  ENTM -> %r за %.0f мс' % (r, (pc() - t) * 1000))
        srv_take(conn, 0.1)
        ser.write(b'LINE-TO-B-TEST\r\n'); after = listen(0.5)
        to_b = srv_take(conn, 0.8)
        out('  строка с линии: в сокет B пришло %r; в линию в ответ %r' % (to_b, after))
        ser.write(rich); after2 = listen(0.6)
        to_b2 = srv_take(conn, 0.8)
        out('  512 Б с CR/LF: в сокет B пришло %d Б; в линию в ответ %r' % (len(to_b2), after2[:60]))
        conn.sendall(b'P2-during-transparent-%d' % cycle)
        on_line = listen(1.0)
        out('  данные сервера во время прозрачного режима: в линию пришло %r' % on_line)
        okexit = leave_transparent()
        lk = val(at('AT+TCPLKB', show=False)); tm = val(at('AT+TMODE', show=False))
        rc = at('AT+RCVB=512,0', show=False)
        out('  после выхода: TCPLKB=%s TMODE=%s RCVB=%r' % (lk, tm, rc))
        results.append({'to_b_line': len(to_b), 'to_b_rich': len(to_b2), 'line_reply': len(after) + len(after2),
                        'server_on_line': len(on_line), 'exit_ok': okexit, 'tcp': lk, 'buffered': b'P2' in rc})
    out('=== 2. в прозрачном режиме: команда без префикса и с префиксом USR')
    at('AT+ENTM')
    r1 = at('AT+TMODE'); r2 = at('USRAT+TMODE'); to_b3 = srv_take(conn, 0.8)
    out('  в сокет B ушло %r' % to_b3)
    leave_transparent()
    out('=== 3. съёмка с отключённым сокетом B: TCPDISB=off,0 -> ENTM -> трафик камеры -> +++/a -> TCPDISB=on,0')
    for cycle in range(1, 4):
        at('AT+TCPDISB=off,0'); time.sleep(0.3)
        out('  TCPLKB после off:', val(at('AT+TCPLKB', show=False)))
        conn.settimeout(2)
        try:
            tail = conn.recv(100); out('  старое соединение: recv ->', tail)
        except Exception as e:
            out('  старое соединение:', type(e).__name__)
        conn.close(); conn = None
        at('AT+ENTM', show=False)
        ser.write(b'CAMERA-WHILE-OFF\r\n'); ser.write(rich); after = listen(0.8)
        out('  мост в линию в ответ на трафик камеры: %r' % after[:60])
        okexit = leave_transparent()
        t = pc(); at('AT+TCPDISB=on,0', show=False)
        lsock.settimeout(10)
        conn, peer = lsock.accept(); conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        dt = pc() - t
        first = srv_take(conn, 1.5)
        out('  выход %s; B снова подключился через %.2f с; на новое соединение пришло %r' % (okexit, dt, first[:60]))
        out('  TCPLKB=%s' % val(at('AT+TCPLKB', show=False)))
        conn.sendall(b'P3-after'); time.sleep(0.1)
        out('  RCVB после переподключения:', at('AT+RCVB=512,0', show=False))
    out('ИТОГ', json.dumps(results, ensure_ascii=False))
finally:
    out('=== возврат сокета B по снимку:', snap['SOCKB'])
    try:
        at('')
        if val(at('AT+TMODE', show=False)) is None:
            leave_transparent()
        at('AT+SOCKB=' + snap['SOCKB'])
        at('AT+Z', wait=0.3)
        time.sleep(8.5)
        at(''); out('после возврата:', {c: val(at('AT+' + c, show=False)) for c in ('UART', 'TMODE', 'NETP', 'SOCKB', 'EVENT', 'UARTTM')})
    finally:
        if conn:
            conn.close()
        lsock.close(); ser.close(); log.close()
