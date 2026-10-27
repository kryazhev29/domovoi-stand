"""Тестовый сервер ДОМОВОЙ NEXT: проверка всего протокола на живой плате (Протокол2).
Плата подключается сюда по TCP (сервер 1 в её настройках). Окно: галочки проверок, «Проверить»,
лента обмена. Запускается ярлыком «Тестовый сервер ДОМОВОЙ» (pythonw, без консоли) и сам при входе
в Windows (ярлык в автозагрузке, --minimized). Второй запуск не стартует второй сервер, а показывает окно.
Управление без окна - ctl.py (порт 127.0.0.1:15011): run all | run c1 c2 key=value | send 22 [hex] |
journal [n] | state | reload (перечитать checks.py, связь с платой не рвётся) | show.
Файлы: protocol.py - кадр и CRC, checks.py - проверки, этот - сервер и окно. Только стандартная библиотека.
Отчёт: Логи/проверка_сервером_<дата>.txt, журнал платы: Логи/журнал_<IMEI>_<дата>.csv,
лента: Логи/тестовый_сервер_<дата>.log."""
import argparse, datetime, importlib, queue, socket, struct, sys, threading, time, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / 'Логи'
PHOTOS = ROOT / 'Фото_с_камеры'

from protocol import parse_answer11, ANSWER, EVENTS, Image, Record, crc16, crc_stm, frame, take_frames, text10, when
import checks
from checks import Checks          # для Tests/test_stand.py; сервер берёт checks.Checks


def answer11_code(payload, command=None):
    """Код ответа 11 (Протокол2: код, время, буфер; старый формат с номером команды тоже) или None."""
    try:
        return parse_answer11(payload, command)[0]
    except (ValueError, struct.error):
        return None


class Board:
    """Сеанс одной платы. Всё входящее - в inbox; проверки ждут нужного через wait()."""
    def __init__(self, say):
        self.say = say
        self.lock = threading.Condition()
        self.conn = None
        self.inbox = []            # (время, uniq, cmd, payload)
        self.journal = []          # Record
        self.temp_ids = []
        self.photos = {}           # номер -> bytearray
        self.completed_photos = set()
        self.reg = None            # разобранная регистрация
        self.status = None         # (время платы, флаги, буфер, время приёма)
        self.uniq = 0
        self.image = None
        self.auto_journal = True
        self.auto_photo = True
        self.busy_journal = self.busy_ids = self.busy_photo = False
        self.connected_at = 0.0
        self.sessions = 0
        self.last_triggers = None
        self.check_lock = threading.Lock()
        self.journal_error = ''
        self.fw_blocks_sent = set()
        self.last_registration = 0.0

    # --- отправка ---
    def send(self, cmd, payload=b'', ttl=255):
        with self.lock:
            if not self.conn:
                raise RuntimeError('плата не подключена')
            self.uniq = (self.uniq + 1) & 255 or 1
            u = self.uniq
            self.conn.sendall(frame(cmd, payload, u, ttl))
            if cmd == 5 and len(payload) == 16:
                self.last_triggers = bytes(payload)
        if cmd not in (20, 24):
            self.say(f'→ команда {cmd} {payload.hex(" ") if len(payload) <= 24 else f"({len(payload)} байт)"}')
        return u

    def wait(self, test, timeout, since=None, stamped=False):
        """Первый пакет (uniq, cmd, payload), для которого test(...) истинно, пришедший после since;
        stamped - вместе со временем приёма: (время, uniq, cmd, payload)."""
        since = time.perf_counter() if since is None else since
        end = time.perf_counter() + timeout
        with self.lock:
            while True:
                for t, u, c, p in self.inbox:
                    if t >= since and test(u, c, p):
                        return (t, u, c, p) if stamped else (u, c, p)
                left = end - time.perf_counter()
                if left <= 0:
                    return None
                self.lock.wait(min(left, 0.5))

    def until(self, test, timeout):
        """Ждать, пока test() станет истинным (проверяется при каждом пакете). Итог test()."""
        end = time.perf_counter() + timeout
        with self.lock:
            while not test():
                left = end - time.perf_counter()
                if left <= 0:
                    break
                self.lock.wait(min(left, 0.5))
            return bool(test())

    def ask(self, cmd, payload=b'', timeout=10, want=11):
        """Команда и ответ на неё (по uniq): (код ответа 11 или None, пакет)."""
        t0 = time.perf_counter()
        u = self.send(cmd, payload)
        # Ответ 11 по Протоколу2 (f431b74): код, время, свободный буфер; номера команды нет — только uniq.
        got = self.wait(lambda uu, c, p: uu == u and c == want and (c != 11 or answer11_code(p, cmd) is not None), timeout, t0)
        if got is None and want != 11:
            got = self.wait(lambda uu, c, p: uu == u and c == 11 and answer11_code(p, cmd) is not None, 0.1, t0)
        if got is None:
            return None, None
        return (answer11_code(got[2], cmd) if got[1] == 11 else 0), got

    # --- приём ---
    def attach(self, conn):
        with self.lock:
            old = self.conn
            self.conn, self.connected_at = conn, time.perf_counter()
            self.sessions += 1
            self.reg = self.status = None
            self.busy_journal = self.busy_ids = self.busy_photo = False
            self.lock.notify_all()
        if old is not None and old is not conn:
            try:
                old.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            old.close()

    def serve(self, conn, attached=False):
        if not attached:
            self.attach(conn)
        conn.settimeout(0.2)
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                except socket.timeout:
                    continue
                frames, buf = take_frames(buf)
                for u, c, p in frames:
                    with self.lock:
                        if self.conn is not conn:
                            return
                        try:
                            self.handle(u, c, p)
                        except (ValueError, IndexError, struct.error) as e:
                            self.say(f'повреждённый пакет {c}: {e}', 'bad')
                            continue
                        self.inbox.append((time.perf_counter(), u, c, p))
                        del self.inbox[:-2000]
                        self.lock.notify_all()
        finally:
            with self.lock:
                if self.conn is conn:
                    self.conn = None
                self.lock.notify_all()

    def handle(self, u, c, p):
        minimum = {1: 37, 2: 8, 3: 21, 4: 2, 5: 10, 6: 200, 8: 11,
                   9: 3, 10: 3, 11: 2, 12: 7, 13: 2, 14: 1, 15: 3}
        if len(p) < minimum.get(c, 0):
            raise ValueError(f'длина {len(p)}, требуется {minimum[c]}')
        if c in (9, 10) and len(p) != 3 + p[2] * (10 if c == 9 else 32):
            raise ValueError('число записей не соответствует длине')
        if c == 1:
            imei = p[:15].decode('ascii', 'replace')
            ts = struct.unpack_from('<5I', p, 17)
            self.reg = dict(imei=imei, protocol=p[15], hw=p[16], id=ts[0], group=ts[1], shedule=ts[2], config=ts[3], holiday=ts[4])
            self.say(f'регистрация: IMEI {imei}, протокол {p[15]}, железо {p[16]}, таймштампы ID {ts[0]} групп {ts[1]} '
                     f'расписаний {ts[2]} настроек {ts[3]} праздников {ts[4]}', 'good')
            self.conn.sendall(frame(1, bytes([0]) + struct.pack('<I', int(time.time())), u))
            self.last_registration = time.perf_counter()
        elif c == 2:
            bt, flags, free = struct.unpack_from('<IHH', p)
            old = self.status[1] if self.status else None
            self.status = (bt, flags, free, time.perf_counter())
            self.conn.sendall(frame(24, bytes([u]), u))
            if flags != old:
                self.say(f'статус 1: флаги {flags:#06x} ({self.flags_text(flags)}), время платы {when(bt)}, буфер {free}')
            if self.auto_journal and not flags & 0x800 and flags & 0x1000 and not self.busy_journal:
                self.busy_journal = True
                self.send(9)
            if self.auto_journal and not flags & 0x800 and flags & 0x4000 and not self.busy_ids:
                self.busy_ids = True
                self.send(10)
            if self.auto_photo and not flags & 0x800 and flags & 0x2000 and not self.busy_photo:
                self.busy_photo = True
                self.send(21, struct.pack('<IH', 0, 0))
        elif c == 3:
            bt, csq, bal, fw, build, boot = struct.unpack_from('<IBHBIB', p)
            self.say(f'статус 2: время {when(bt)}, сигнал {csq}, баланс {bal}, FW {fw} сборка {build}, '
                     f'загрузчик {boot:#x}, периферия {p[13:21].hex(" ")}'
                     + (f', входы {p[21]}/{p[22]}, выходы {p[23]}/{p[24]}, оператор {p[25]}, сеть {p[26]}' if len(p) >= 27 else ''))
        elif c == 4:
            self.say(f'обновление: {p[0]}%, код {p[1]}' + (' - ЗАГРУЗКА ПРЕКРАЩЕНА' if p[1] == 253 else ''),
                     'bad' if p[1] == 253 else 'info')
        elif c == 5:
            v = struct.unpack_from('<5H', p)
            self.say('CRC структур: ID {:#06x} групп {:#06x} расписаний {:#06x} настроек {:#06x} праздников {:#06x}'.format(*v))
        elif c == 6:
            self.say(f'CRC блоков ID: {len(p) // 2} шт., первые {p[:8].hex(" ")}')
        elif c == 7:
            sms = p.split(bytes(1))[0].decode('cp1251', 'replace')
            self.say(f'пакет 7, SMS от платы: «{sms}»', 'good')
        elif c == 8:
            self.say(f'пакет 8, ID со считывателя: код {p[0]} ' + ('(считан)' if p[0] == 1 else '(время вышло)') +
                     f' «{text10(p[1:11])}»')
        elif c == 9:
            rest, n = struct.unpack_from('<HB', p) if len(p) >= 3 else (0, 0)
            ids = [text10(p[3 + i * 10:13 + i * 10]) for i in range(n)]
            self.temp_ids += ids
            self.say(f'временные ID: {n} в пакете, осталось {rest}: {", ".join(ids)}')
            if n:
                self.send(10)               # следующий запрос - подтверждение этих
            else:
                self.busy_ids = False
        elif c == 10:
            rest, n = struct.unpack_from('<HB', p)
            recs = [Record(p[3 + i * 32:35 + i * 32]) for i in range(n)]
            self.journal += recs
            for r in recs:
                self.say(f'журнал: {r}', 'event')
            self.save_journal(recs)
            if n:
                self.send(9)                # повтор команды подтверждает принятое
            else:
                self.busy_journal = False
                self.say(f'журнал выгружен, всего записей за сеанс сервера: {len(self.journal)}')
        elif c == 11:
            code = answer11_code(p)
            self.say(f'ответ 11, uniq {u}: {ANSWER.get(code, code)} ({code})', 'good' if code == 0 else 'bad')
        elif c == 12:
            kind, ts, crc = struct.unpack_from('<BIH', p)
            names = {1: 'ID', 2: 'групп', 3: 'расписаний', 4: 'настроек', 5: 'праздников', 255: 'ОШИБКА'}
            self.say(f'таймштамп {names.get(kind, kind)}: {ts}, CRC {crc:#06x}')
        elif c == 13:
            n = struct.unpack_from('<H', p)[0]
            if self.image and n < self.image.blocks:
                self.conn.sendall(frame(20, self.image.block(n), u))
                self.fw_blocks_sent.add(n)
                if n % 10 == 0 or n == self.image.blocks - 1:
                    self.say(f'блок прошивки {n}/{self.image.blocks - 1} отдан')
            else:
                self.say(f'плата просит блок {n}, прошивки нет', 'bad')
        elif c == 14:
            self.photo_packet(p)
        elif c == 15:
            self.say(f'CRC блока ID {p[0]}: {struct.unpack_from("<H", p, 1)[0]:#06x}')
        else:
            self.say(f'пакет {c}, {len(p)} байт: {p[:32].hex(" ")}')

    def photo_packet(self, p):
        if not p or p[0] == 0:
            self.busy_photo = False
            self.say('фото: очередь пуста')
            return
        num, size, off = struct.unpack_from('<IIH', p, 1)
        data = self.photos.setdefault(num, bytearray())
        if off == len(data):
            data += p[11:]
        if len(data) >= size:
            PHOTOS.mkdir(exist_ok=True)
            name = PHOTOS / f'{self.reg["imei"] if self.reg else "плата"}_{num}.jpg'
            name.write_bytes(bytes(data[:size]))
            self.completed_photos.add(num)
            self.say(f'фото №{num} получено, {size} байт -> {name.name}', 'good')
            self.send(21, struct.pack('<IH', num, size))   # итоговое подтверждение

    @staticmethod
    def flags_text(f):
        names = {15: 'заводские', 14: 'врем.ID', 13: 'фото', 12: 'журнал', 11: 'обновление', 7: 'ручн.разбл',
                 6: 'разбл.по расп', 5: 'авторег', 4: 'открыв.всем', 3: 'вход1', 2: 'вход2', 1: 'выход1', 0: 'выход2'}
        on = [n for b, n in names.items() if f >> b & 1]
        return ', '.join(on) or 'ничего'

    def save_journal(self, recs):
        if not recs:
            return
        LOGS.mkdir(exist_ok=True)
        imei = self.reg['imei'] if self.reg else 'плата'
        path = LOGS / f'журнал_{imei}_{datetime.date.today():%Y-%m-%d}.csv'
        new = not path.exists()
        with path.open('a', encoding='utf-8-sig') as f:
            if new:
                f.write('номер;время UTC;событие;название;аргумент 1;аргумент 2;проходы\n')
            for r in recs:
                f.write(f'{r.index};{when(r.time)};{r.type};{EVENTS.get(r.type, "?")};{r.arg_text()};{r.arg2};{r.passes}\n')

    def events_since(self, t0):
        """Номера событий журнала, пришедших после t0 (time.time платы)."""
        return [r.type for r in self.journal if r.time >= t0 - 5]

    def pull_journal(self, timeout=40):
        """Забрать журнал до пустого ответа."""
        if not self.conn:
            self.journal_error = 'нет связи с платой; проверьте подключение'
            return False
        t0 = time.perf_counter()
        self.busy_journal = True
        self.send(9)
        end = time.perf_counter() + timeout
        try:
            while time.perf_counter() < end and self.conn:
                got = self.wait(lambda u, c, p: c == 10 and len(p) >= 3 and p[2] == 0,
                                min(2, max(0, end-time.perf_counter())), t0)
                if got:
                    self.journal_error = ''
                    return True
            records = any(t >= t0 and c == 10 and p[2] for t, u, c, p in self.inbox)
            self.journal_error = ('нет связи; проверьте TCP' if not self.conn else
                                  'записи идут, но выгрузка не закончилась; проверьте курсор подтверждения' if records else
                                  'плата не отвечает на команду 9; проверьте журнал и бит 11')
        finally:
            self.busy_journal = False
        return False




class Server(threading.Thread):
    daemon = True

    def __init__(self, port, say, host='0.0.0.0'):
        super().__init__()
        self.port, self.say = port, say
        self.board = Board(say)
        self.stopping = False
        self.host = host
        self.ready = threading.Event()
        self.error = None
        self.listener = None

    def stop(self):
        self.stopping = True
        with self.board.lock:
            conn = self.board.conn
            self.board.conn = None
            self.board.lock.notify_all()
        for s in (conn, self.listener):
            if s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    def client(self, conn):
        try:
            self.board.serve(conn, attached=True)
        except OSError as e:
            if not self.stopping:
                self.say(f'сеанс закрыт: {e}')
        finally:
            conn.close()

    def run(self):
        srv = socket.socket()
        self.listener = srv
        # Windows: SO_REUSEADDR lets a second (stale) server share the port and steal the
        # board; exclusive use makes a second instance fail loudly instead.
        srv.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', socket.SO_REUSEADDR), 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as e:
            self.say(f'порт {self.port} занят: {e}', 'bad')
            self.error = str(e)
            self.ready.set()
            srv.close()
            return
        srv.listen(8)
        self.port = srv.getsockname()[1]
        srv.settimeout(0.5)
        self.ready.set()
        self.say(f'слушаем порт {self.port}, ждём плату')
        while not self.stopping:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping:
                    break
                raise
            self.say(f'плата подключилась с {addr[0]}', 'good')
            self.board.attach(conn)
            threading.Thread(target=self.client, args=(conn,), daemon=True).start()
        srv.close()


CONTROL = ('127.0.0.1', 15011)


def control(line, timeout=5):
    """Строка команды работающему серверу -> строки ответа; None, если сервер не запущен."""
    try:
        with socket.create_connection(CONTROL, timeout=timeout) as s:
            s.settimeout(None)
            s.sendall((line + '\n').encode('utf-8'))
            data = b''
            while not data.endswith(b'END\n'):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return None
    return data.decode('utf-8').removesuffix('END\n').rstrip('\n').split('\n')


class Control(threading.Thread):
    """127.0.0.1:15011: строка команды -> строки ответа и END. Каждое подключение - свой поток:
    долгий прогон не мешает спросить state."""
    daemon = True

    def __init__(self, server, opts, say, show=None):
        super().__init__()
        self.server, self.opts, self.say, self.show = server, opts, say, show
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', socket.SO_REUSEADDR), 1)
        self.sock.bind(CONTROL)             # занят - OSError в main: второй сервер не стартует
        self.sock.listen(4)

    def run(self):
        while True:
            c, _ = self.sock.accept()
            threading.Thread(target=self.client, args=(c,), daemon=True).start()

    def client(self, c):
        with c:
            line = c.makefile(encoding='utf-8').readline().strip()
            try:
                out = self.do(line)
            except Exception as e:      # noqa: BLE001
                out = [f'ошибка: {e!r}']
            try:
                c.sendall(('\n'.join(out) + '\nEND\n').encode('utf-8'))
            except OSError:
                pass

    def do(self, line):
        b = self.server.board
        words = line.split()
        if not words:
            return []
        if words[0] == 'run':
            names = Checks.SAFE if words[1:] in (['all'], []) else words[1:]
            for kv in [w for w in names if '=' in w]:
                k, v = kv.split('=', 1)
                self.opts[k] = v
            names = [w for w in names if '=' not in w]
            return [f'{"ГОДНО" if ok else "НЕ ГОДНО"} {n}: {t}'
                    for n, ok, t in checks.Checks(b, self.say, self.opts).run(names)]
        if words[0] == 'reload':
            return [reload_checks(self.server, self.say)]
        if words[0] == 'show':
            if self.show:
                self.show()
                return ['окно показано']
            return ['сервер без окна (--headless)']
        if words[0] == 'send':
            t0 = time.perf_counter()
            u = b.send(int(words[1]), bytes.fromhex(''.join(words[2:])))
            got = b.wait(lambda uu, c, p: uu == u, 15, t0)
            return [f'пакет {got[1]}: {got[2].hex(" ")}' if got else 'ответа нет за 15 с']
        if words[0] == 'journal':
            b.pull_journal()
            return [str(r) for r in b.journal[-int(words[1]) if len(words) > 1 else -50:]]
        if words[0] == 'state':
            st = b.status
            return [f'подключена: {bool(b.conn)}, сеансов {b.sessions}, IMEI {b.reg and b.reg["imei"]}',
                    f'статус: {st and hex(st[1])} ({st and Board.flags_text(st[1])}), записей журнала {len(b.journal)}']
        return ['команды: run all | run full | run <проверки> [phone=79..] [config=файл] [firmware=файл] | '
                'send <cmd> <hex> | journal [n] | state | reload | show',
                'проверки: ' + ' '.join(checks.Checks.SAFE + checks.Checks.RISKY)]


def reload_checks(server, say):
    """Перечитать checks.py без перезапуска: связь с платой, журнал и лента остаются."""
    if not server.board.check_lock.acquire(blocking=False):
        return 'идёт прогон - перечитать после него'
    try:
        importlib.reload(checks)
    except Exception as e:      # noqa: BLE001 - ошибка в файле проверок не роняет сервер
        say(f'checks.py не перечитан: {e!r}', 'bad')
        return f'ошибка в checks.py, остались прежние проверки: {e!r}'
    finally:
        server.board.check_lock.release()
    say('проверки перечитаны из checks.py', 'head')
    return 'проверки перечитаны'



def gui(server, opts, log_q, say, minimized=False):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title('ДОМОВОЙ NEXT - тестовый сервер')
    root.geometry('1100x760')
    left = ttk.Frame(root, padding=8)
    left.pack(side='left', fill='y')
    state = ttk.Label(left, font=('Segoe UI', 10, 'bold'))
    state.pack(anchor='w', pady=(0, 8))
    boxes = ttk.Frame(left)
    boxes.pack(anchor='w', fill='x')
    vars_ = {}

    def fill_boxes():
        """Галочки из текущего checks.py (после «Перечитать проверки» - новые)."""
        old = {n: v.get() for n, v in vars_.items()}
        for w in boxes.winfo_children():
            w.destroy()
        vars_.clear()
        C = checks.Checks
        ttk.Label(boxes, text='Проверки (без риска)', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        for n in C.SAFE:
            vars_[n] = tk.BooleanVar(value=old.get(n, True))
            ttk.Checkbutton(boxes, text=f'{n}: {C.TITLES[n]}', variable=vars_[n]).pack(anchor='w')
        ttk.Label(boxes, text='Меняют плату - по галочке', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(10, 0))
        for n in C.RISKY:
            vars_[n] = tk.BooleanVar(value=old.get(n, False))
            ttk.Checkbutton(boxes, text=f'{n}: {C.TITLES[n]}', variable=vars_[n]).pack(anchor='w')
    fill_boxes()
    fields = {}
    for key, label in (('phone', 'Телефон для SMS (10 цифр)'), ('config', 'Файл настроек платы (read_config.py)'),
                       ('firmware', 'Файл прошивки 139.71.<сборка>.bin'), ('camera', 'Адрес камеры')):
        ttk.Label(left, text=label).pack(anchor='w', pady=(6, 0))
        row = ttk.Frame(left)
        row.pack(fill='x')
        e = ttk.Entry(row, width=48)
        e.pack(side='left')
        if key in ('config', 'firmware'):
            ttk.Button(row, text='…', width=3, command=lambda e=e: (e.delete(0, 'end'), e.insert(0, filedialog.askopenfilename()))).pack(side='left')
        fields[key] = e

    def start():
        for k, e in fields.items():
            if e.get().strip():
                opts[k] = e.get().strip()
        names = [n for n, v in vars_.items() if v.get()]
        threading.Thread(target=lambda: checks.Checks(server.board, say, opts).run(names), daemon=True).start()

    def reload_():
        threading.Thread(target=lambda: log_q.put(('reloaded', reload_checks(server, say))), daemon=True).start()

    def close():
        if messagebox.askokcancel('Тестовый сервер', 'Закрыть сервер? Плата 1 останется без сервера до следующего запуска.'):
            server.stop()
            root.destroy()
    ttk.Button(left, text='Проверить отмеченное', command=start).pack(fill='x', pady=10)
    ttk.Button(left, text='Забрать журнал', command=lambda: threading.Thread(target=server.board.pull_journal, daemon=True).start()).pack(fill='x')
    ttk.Button(left, text='Перечитать проверки (checks.py)', command=reload_).pack(fill='x', pady=(6, 0))
    text = tk.Text(root, wrap='word', font=('Consolas', 10))
    text.pack(side='left', fill='both', expand=True)
    for tag, color in (('good', '#0a7d22'), ('bad', '#b3261e'), ('head', '#1d4ed8'), ('event', '#6b21a8')):
        text.tag_config(tag, foreground=color)

    def show():
        root.deiconify()
        root.state('normal')
        root.lift()
        root.focus_force()

    def pump():
        while True:
            try:
                kind, line = log_q.get_nowait()
            except queue.Empty:
                break
            if kind == 'show':
                show()
            elif kind == 'reloaded':
                fill_boxes()
            else:
                text.insert('end', f'{datetime.datetime.now():%H:%M:%S} {line}\n', kind)
                text.see('end')
        b = server.board
        state.config(text=f'Плата на связи: IMEI {b.reg["imei"]}, сеанс {b.sessions}' if b.conn and b.reg else
                     'Плата не подключена: ждём на порту ' + str(server.port),
                     foreground='#0a7d22' if b.conn and b.reg else '#b3261e')
        root.after(200, pump)
    root.protocol('WM_DELETE_WINDOW', close)
    if minimized:
        root.iconify()
    pump()
    root.mainloop()


def main():
    global LOGS, PHOTOS
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=15010)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--minimized', action='store_true', help='окно свёрнуто (автозапуск)')
    ap.add_argument('--run', nargs='+', help='один прогон: all — обычные, full — включая изменение платы')
    ap.add_argument('--wait-board', type=float, default=60)
    ap.add_argument('--firmware')
    ap.add_argument('--config')
    ap.add_argument('--server1', help='адрес:порт этого сервера для синхронизации настроек')
    ap.add_argument('--phone', default='')
    ap.add_argument('--camera', default=1, type=int)
    ap.add_argument('--output', '-o')
    ap.add_argument('--json')
    ap.add_argument('--log-dir')
    ap.add_argument('--update-timeout', type=float, default=600)
    a = ap.parse_args()
    if a.log_dir:
        LOGS = Path(a.log_dir)
        PHOTOS = LOGS / 'photos'
    LOGS.mkdir(parents=True, exist_ok=True)
    log_q, lock = queue.Queue(), threading.Lock()
    if not a.run and control('show') is not None:
        print('тестовый сервер уже работает - окно показано')
        return

    def say(line, kind='info'):
        now = datetime.datetime.now()
        with lock, (LOGS / f'тестовый_сервер_{now:%Y-%m-%d}.log').open('a', encoding='utf-8') as f:
            f.write(f'{now:%H:%M:%S} {line}\n')
        if a.headless:
            print(f'{now:%H:%M:%S} {line}', flush=True)
        else:
            log_q.put((kind, line))
    # Без консоли (pythonw) упавший поток иначе исчезает молча.
    threading.excepthook = lambda e: say(f'ошибка в потоке {e.thread and e.thread.name}: {e.exc_value!r}', 'bad')
    opts = {k: v for k, v in vars(a).items() if v is not None}
    opts['log_dir'] = str(LOGS)
    ctl = None
    if not a.run:
        try:
            ctl = Control(None, opts, say, None if a.headless else lambda: log_q.put(('show', '')))
        except OSError as e:
            fail(a, f'порт управления {CONTROL[1]} занят другой программой: {e}')
    server = Server(a.port, say, a.bind)
    server.start()
    server.ready.wait(5)
    if server.error or not server.ready.is_set():
        fail(a, f'порт {a.port} для платы занят: {server.error}', 2)
    if a.run:
        try:
            b = server.board
            b.until(lambda: b.reg and b.status, a.wait_board)
            C = checks.Checks
            names = C.SAFE + C.RISKY if a.run == ['full'] else C.SAFE if a.run == ['all'] else a.run
            results = C(b, say, opts).run(names)
            if not b.reg and a.json:
                Path(a.json).write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8')
            raise SystemExit(0 if results and all(ok for _, ok, _ in results) else 1)
        finally:
            server.stop()
    ctl.server = server
    ctl.start()
    if a.headless:
        while True:
            time.sleep(3600)
    else:
        gui(server, opts, log_q, say, a.minimized)


def fail(a, text, code=1):
    """Ошибка запуска: в консоль или, без консоли (pythonw), окном."""
    if a.headless or sys.stdout is not None:
        print(text, file=sys.stderr)
    else:
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showerror('Тестовый сервер', text)
    raise SystemExit(code)


if __name__ == '__main__':
    main()
