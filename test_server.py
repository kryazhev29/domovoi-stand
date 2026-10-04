"""Тестовый сервер ДОМОВОЙ NEXT: проверка всего протокола на живой плате (Протокол2, 04.10.2026).
Плата подключается сюда по TCP (сервер 1 в её настройках). Окно: галочки проверок, «Проверить»,
лента обмена. Управляющий порт 127.0.0.1:15011 (строки: run all | run c1 c2 | send 22 [hex] |
journal | state) - чтобы проверки можно было запускать без окна. Только стандартная библиотека.
Запуск: py -3.11 test_server.py [--headless] [--port 15010]
Отчёт: Логи/проверка_сервером_<дата>.txt, журнал платы: Логи/журнал_<IMEI>_<дата>.csv."""
import argparse, datetime, queue, socket, struct, threading, time, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / 'Логи'
PHOTOS = ROOT / 'Фото_с_камеры'

# Лист «Журнал событий»: номер -> название
EVENTS = {
    0: 'заводские настройки', 6: 'синхронизация ID завершена', 7: 'синхронизация настроек завершена',
    8: 'синхронизация групп завершена', 9: 'синхронизация расписаний завершена', 10: 'синхронизация праздников завершена',
    11: 'нет регистрации в сети', 12: 'нет интернета', 13: 'нет трафика до сервера',
    14: 'подключён к серверу 1', 15: 'подключён к серверу 2', 16: 'ошибка соединения с сервером 1',
    17: 'ошибка соединения с сервером 2', 18: 'потеря связи с сервером 1', 19: 'потеря связи с сервером 2',
    20: 'вход 1 активен', 21: 'вход 1 выключен', 22: 'вход 2 активен', 23: 'вход 2 выключен',
    24: 'авторегистрация вкл', 25: 'авторегистрация выкл', 26: 'открывать всем вкл', 27: 'открывать всем выкл',
    28: 'разблокировка по расписанию вкл', 29: 'разблокировка по расписанию выкл',
    30: 'ручная разблокировка вкл', 31: 'ручная разблокировка выкл',
    32: 'перезагрузка по суточному таймеру', 33: 'перезагрузка по команде', 34: 'перезагрузка по расписанию',
    35: 'входящее SMS', 36: 'отправлено SMS', 37: 'баланс ниже нормы', 38: 'низкий сигнал',
    39: 'перезагрузка по питанию',
    65: 'ID нет в базе', 66: 'ID заблокирован', 67: 'ID добавлен по SMS', 68: 'ID добавлен авторегистрацией',
    69: 'выход по телефону', 70: 'выход по RFID 485', 71: 'выход по Wiegand', 72: 'выход по брелку',
    73: 'выход по автономеру', 74: 'выход: тест из админки', 75: 'выход: API', 76: 'выход: голосовой помощник',
    77: 'выход: мобильное приложение', 78: 'ID со считывателя по запросу', 79: 'проходы суток закончились',
    129: 'фотофиксация', 131: 'счётчик проходов', 132: 'начало загрузки FW', 133: 'сбой загрузки FW',
    134: 'загрузка FW: 10 минут вышли', 135: 'FW загружена, установка', 136: 'обновление FW завершено',
    137: 'откат FW', 138: 'время действия ID истекло', 139: 'время действия ID началось',
}
ANSWER = {0: 'выполнено', 1: 'ошибка в параметре', 2: 'ошибка выполнения', 3: 'неверный результат',
          6: 'устарела', 254: 'ошибка CRC', 255: 'не поддерживается'}


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


def frame(cmd, payload, uniq, ttl=255):
    body = bytes([ttl, uniq, cmd]) + payload
    lb = struct.pack('<H', len(body))
    return b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))


def take_frames(buf):
    """Целые кадры из потока: [(uniq, cmd, payload)], остаток."""
    out, p = [], 0
    while True:
        i = buf.find(b'SLDN', p)
        if i < 0:
            p = max(p, len(buf) - 3)
            break
        if len(buf) < i + 6:
            p = i
            break
        ln = struct.unpack_from('<H', buf, i + 4)[0]
        if ln < 3 or ln > 2048:
            p = i + 4
            continue
        if len(buf) < i + 8 + ln:
            p = i
            break
        if struct.unpack_from('<H', buf, i + 6 + ln)[0] == crc16(buf[i + 4:i + 6 + ln]):
            out.append((buf[i + 7], buf[i + 8], bytes(buf[i + 9:i + 6 + ln])))
            p = i + 8 + ln
        else:
            p = i + 4
    return out, buf[p:]


def when(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%d.%m %H:%M:%S') if t else '-'


def text10(b):
    return b.split(b'\0')[0].decode('cp1251', 'replace')


class Record:
    """Запись журнала (32 байта, пакет 10)."""
    def __init__(self, raw):
        self.index, self.time, self.type = struct.unpack_from('<IIB', raw)
        self.arg1, self.arg2, self.passes = raw[9:19], struct.unpack_from('<I', raw, 19)[0], raw[23]

    def arg_text(self):
        if self.type == 129:
            return f'фото №{struct.unpack_from("<I", self.arg1)[0]}'
        if 132 <= self.type <= 137:
            hw, fw, build = struct.unpack_from('<BHI', self.arg1)
            return f'FW {hw}.{fw}.{build}, причина {self.arg2}'
        return text10(self.arg1)

    def __str__(self):
        name = EVENTS.get(self.type, '?')
        extra = f', проходы {self.passes}' if self.passes != 255 and self.type >= 64 else ''
        return f'#{self.index} {when(self.time)} [{self.type}] {name} «{self.arg_text()}»{extra}'


class Image:
    def __init__(self, path):
        self.path = Path(path)
        _, fw, stamp = (int(x) for x in self.path.name.split('.')[:3])
        body = self.path.read_bytes()
        body += b'\xff' * (-len(body) % 1024)
        self.fw, self.stamp, self.body = fw, stamp, body
        self.blocks = len(body) // 1024
        self.crc = crc_stm(body)

    def cmd19(self):
        return struct.pack('<HIIIH', self.fw, self.stamp, len(self.body), self.crc, self.blocks)

    def block(self, n):
        return struct.pack('<H', n) + self.body[n * 1024:(n + 1) * 1024].ljust(1024, b'\xff')


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

    def wait(self, test, timeout, since=None):
        """Первый пакет (uniq, cmd, payload), для которого test(...) истинно, пришедший после since."""
        since = time.perf_counter() if since is None else since
        end = time.perf_counter() + timeout
        with self.lock:
            while True:
                for t, u, c, p in self.inbox:
                    if t >= since and test(u, c, p):
                        return u, c, p
                left = end - time.perf_counter()
                if left <= 0:
                    return None
                self.lock.wait(min(left, 0.5))

    def ask(self, cmd, payload=b'', timeout=10, want=11):
        """Команда и ответ на неё (по uniq): (код ответа 11 или None, пакет)."""
        t0 = time.perf_counter()
        u = self.send(cmd, payload)
        got = self.wait(lambda uu, c, p: uu == u and c == want and
                        (c != 11 or len(p) >= 2 and p[0] == cmd), timeout, t0)
        if got is None and want != 11:
            got = self.wait(lambda uu, c, p: uu == u and c == 11 and len(p) >= 2 and p[0] == cmd, 0.1, t0)
        if got is None:
            return None, None
        return (got[2][1] if got[1] == 11 else 0), got

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
                     f'загрузчик {boot:#x}, периферия {p[13:21].hex(" ")}')
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
            self.say(f'ответ на команду {p[0]}: {ANSWER.get(p[1], p[1])} ({p[1]})', 'good' if p[1] == 0 else 'bad')
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


class Checks:
    """Проверки протокола. Каждая: (ok, текст). Проверка не трогает чужое без галочки."""
    SAFE = ['связь', 'время', 'статус2', 'crc', 'crc_блоки', 'неизвестная', 'триггеры', 'выходы', 'считыватель',
            'временные_id', 'журнал']
    RISKY = ['синхронизация', 'праздники', 'проходы', 'sms', 'фото', 'перезагрузка',
             'связь_после_перезагрузки', 'обновление']
    TITLES = {
        'связь': 'регистрация, статус 1 каждые 15 с, подтверждение 24',
        'время': 'команда 23 - установка времени',
        'статус2': 'команда 22 - статус 2',
        'crc': 'команда 17 - CRC пяти структур',
        'crc_блоки': 'команда 18 - CRC блоков ID (все, один, неверный номер)',
        'неизвестная': 'команда 27 и несуществующая 99',
        'триггеры': 'команда 5 - триггеры, события 24-31',
        'выходы': 'команда 8 - выходы, события 74-77 (реле щёлкает)',
        'считыватель': 'команда 6 - запрос ID со считывателя, пакет 8, событие 78 при карте',
        'временные_id': 'команда 10 - ID по SMS и авторегистрации',
        'журнал': 'команда 9 - журнал, события этого прогона',
        'синхронизация': 'команды 12-16 - синхронизация, события 6-10 (перезаписывает таблицы)',
        'проходы': 'ID с лимитом проходов + команда 8: события 131 и 79 (после синхронизации)',
        'sms': 'команда 11 - SMS на номер, событие 36',
        'фото': 'команда 7 - снимок, фото командой 21, событие 129',
        'перезагрузка': 'команда 2 - перезагрузка, события 33 и 14',
        'обновление': 'команда 19 - прошивка из файла, события 132, 135, 136',
        'праздники': 'команда 16: три даты, затем одна; замена списка и CRC',
        'связь_после_перезагрузки': 'команда 2: регистрация после перезапуска быстрее 60 с',
    }

    def __init__(self, board, say, opts):
        self.b, self.say, self.opts = board, say, opts
        self.expected = []          # (название проверки, номер события)
        self.t0 = 0

    def run(self, names):
        if not self.b.check_lock.acquire(blocking=False):
            return [('занято', False, 'другой прогон ещё не закончен')]
        try:
            return self._run(list(dict.fromkeys(names)))
        finally:
            self.b.check_lock.release()

    def _run(self, names):
        b = self.b
        if not b.conn:
            results = [('подключение', False, 'плата не подключена; проверьте адрес и порт сервера')]
            self.report(results)
            return results
        self.t0 = (b.status[0] if b.status else int(time.time()))
        results = []
        names = [n for n in names if n not in ('обновление', 'журнал')] + \
            [n for n in ('обновление', 'журнал') if n in names]
        for n in names:
            if n == 'журнал':
                continue
            self.say(f'=== проверка «{n}»: {self.TITLES.get(n, "")}', 'head')
            try:
                ok, text = getattr(self, 'c_' + n)()
            except Exception as e:     # noqa: BLE001 - проверка не роняет сервер
                ok, text = False, f'исключение: {e!r}'
            results.append((n, ok, text))
            self.say(f'{"ГОДНО" if ok else "НЕ ГОДНО"}: {n} - {text}', 'good' if ok else 'bad')
        if 'журнал' in names:
            self.say('=== проверка «журнал»: все ожидаемые события этого прогона', 'head')
            if b.status and b.status[1] & 0x800:
                ok, text = False, 'бит 11 ещё установлен; дождитесь завершения обновления'
            else:
                try:
                    ok, text = self.c_журнал()
                except (OSError, RuntimeError) as e:
                    ok, text = False, f'связь прервалась при выгрузке журнала: {e}; проверьте TCP'
            results.append(('журнал', ok, text))
            self.say(f'{"ГОДНО" if ok else "НЕ ГОДНО"}: журнал - {text}', 'good' if ok else 'bad')
        self.report(results)
        return results

    def expect(self, check, *events):
        self.expected += [(check, e) for e in events]

    def answer(self, cmd, payload=b'', want_code=0, timeout=10):
        code, _ = self.b.ask(cmd, payload, timeout)
        return code == want_code, code

    # --- безопасные ---
    def c_связь(self):
        b = self.b
        if not b.reg:
            return False, 'регистрации не было'
        r = b.reg
        bad = []
        if len(r['imei']) != 15 or not r['imei'].isdigit():
            bad.append(f'IMEI «{r["imei"]}»')
        t0 = time.perf_counter()
        s1 = b.wait(lambda u, c, p: c == 2, 20, t0)
        s2 = s1 and b.wait(lambda u, c, p: c == 2, 20, time.perf_counter() + 0.01)
        if not (s1 and s2):
            return False, 'статус 1 не приходит дважды за 40 с'
        times = [t for t, u, c, p in b.inbox if c == 2 and t >= t0]
        gap = times[-1] - times[-2] if len(times) >= 2 else 0
        if not 12 <= gap <= 18:
            bad.append(f'статус через {gap:.1f} с, а не 15')
        return not bad, f'IMEI {r["imei"]}, железо {r["hw"]}, протокол {r["protocol"]}, статус каждые {gap:.1f} с' + \
            (f'; замечания: {"; ".join(bad)}' if bad else '')

    def c_время(self):
        b = self.b
        now = int(time.time())
        ok, code = self.answer(23, struct.pack('<I', now))
        if not ok:
            return False, f'ответ {code}'
        t0 = time.perf_counter()
        got = b.wait(lambda u, c, p: c == 2, 20, t0)
        if not got:
            return False, 'после команды 23 нет статуса'
        diff = struct.unpack_from('<I', got[2])[0] - int(time.time())
        return abs(diff) <= 3, f'время платы отличается на {diff} с'

    def c_статус2(self):
        code, got = self.b.ask(22, b'', 15, want=3)
        if not got or got[1] != 3:
            return False, f'нет пакета 3 (ответ {code})'
        bt, csq, bal, fw, build, boot = struct.unpack_from('<IBHBIB', got[2])
        bad = []
        if len(got[2]) != 21:
            bad.append(f'длина {len(got[2])}, а не 21')
        if boot != 0x32:
            bad.append(f'загрузчик {boot:#x}')
        if not 0 < csq < 32:
            bad.append(f'сигнал {csq}')
        return not bad, f'FW {fw}, сборка {build}, сигнал {csq}, баланс {bal}' + (f'; {"; ".join(bad)}' if bad else '')

    def c_crc(self):
        code, got = self.b.ask(17, b'', 15, want=5)
        return bool(got and got[1] == 5 and len(got[2]) == 10), 'пакет 5 пришёл' if got and got[1] == 5 else f'нет пакета 5 ({code})'

    def c_crc_блоки(self):
        _, all_ = self.b.ask(18, bytes([0]), 20, want=6)
        _, one = self.b.ask(18, bytes([1]), 15, want=15)
        code_bad, _ = self.b.ask(18, bytes([101]), 10)
        ok = bool(all_ and all_[1] == 6 and len(all_[2]) == 200 and one and one[1] == 15 and one[2][0] == 1 and code_bad == 1)
        return ok, f'все блоки: {"есть" if all_ and all_[1] == 6 else "нет"}, блок 1: {"есть" if one and one[1] == 15 else "нет"}, ' \
                   f'блок 101: ответ {code_bad} (нужно 1)'

    def c_неизвестная(self):
        c27, _ = self.b.ask(27, bytes([1, 0, 0]), 10)
        c99, _ = self.b.ask(99, b'', 10)
        return c27 is not None and c99 == 255, f'27: ответ {c27}, 99: ответ {c99} (нужно 255)'

    def c_триггеры(self):
        b = self.b
        if not b.status:
            return False, 'нет статуса'
        f = b.status[1]
        cur = [f >> 7 & 1, f >> 6 & 1, f >> 5 & 1, f >> 4 & 1]       # ручная, по расписанию, авторег, всем
        if b.last_triggers:
            cur[1] = b.last_triggers[1]
        tail = b.last_triggers[4:] if b.last_triggers else bytes(12)
        warning = '' if b.last_triggers else '; ПРЕДУПРЕЖДЕНИЕ: триггеры 5–8 неизвестны, отправлены нули; проверьте SMS-настройки'
        flip = cur[:]
        flip[2] ^= 1
        def payload(t):
            return bytes(t) + tail
        t0 = time.perf_counter()
        ok1, code1 = self.answer(5, payload(flip))
        st = b.wait(lambda u, c, p: c == 2 and (struct.unpack_from('<H', p, 4)[0] >> 5 & 1) == flip[2], 20, t0)
        ok2, code2 = self.answer(5, payload(cur))
        self.expect('триггеры', 24 if flip[2] else 25, 24 if cur[2] else 25)
        return ok1 and ok2 and bool(st), f'авторегистрация {cur[2]}→{flip[2]}→{cur[2]}: ответы {code1}, {code2}, ' \
                   f'статус {"показал" if st else "НЕ показал"} смену' + warning

    def c_выходы(self):
        res = []
        allok = True
        for src, ev in ((3, 74), (2, 75), (4, 76), (1, 77)):
            before = self.b.status
            t0 = time.perf_counter()
            code, reply = self.b.ask(8, bytes([src, 1, 0]))
            ok = code == 0
            if reply:
                # Статус до ответа 11 может описывать состояние до команды.
                t0 = max((t for t, u, c, p in self.b.inbox
                          if t >= t0 and (u, c, p) == reply), default=t0)
            reason = ''
            if code == 2:
                reason = ('выход занят' if before and before[1] & 2 else
                          'реле не разрешено командам сервера: проверьте настройки' if before and t0-before[3] <= 20 else
                          'нет свежего статуса, причина отказа не определена')
            off = self.output_off(since=t0)
            allok &= ok and off
            res.append(f'источник {src}: {code}' + (f' ({reason})' if reason else '') +
                       ('' if off else ' — выход не выключился за 60 с'))
            if ok:
                self.expect('выходы', ev)
        ok, code = self.answer(8, bytes([3, 2, 0]), want_code=1)
        res.append(f'выход=2: {code} (нужно 1)')
        return allok and ok, '; '.join(res)

    def output_off(self, timeout=60, since=None):
        """Ждать, пока статус 1 покажет выход 1 выключенным: включённый импульс не перезапускается."""
        t0 = time.perf_counter() if since is None else since
        if self.b.status and self.b.status[3] >= t0 and not self.b.status[1] & 2:
            return True
        return bool(self.b.wait(lambda u, c, p: c == 2 and not struct.unpack_from('<H', p, 4)[0] & 2, timeout, t0))

    def c_считыватель(self):
        b = self.b
        wait_s = int(self.opts.get('reader_wait', 8))
        ok, code = self.answer(6, bytes([wait_s, 1, 0]))
        if not ok:
            return False, f'ответ на 6: {code}'
        self.say(f'ждём {wait_s} с: можно приложить карту к считывателю Wiegand')
        got = b.wait(lambda u, c, p: c == 8, wait_s + 5)
        if not got:
            return False, 'пакет 8 не пришёл'
        if got[2][0] == 1:
            self.expect('считыватель', 78)
            return True, f'карта считана: {text10(got[2][1:11])}'
        bad, _ = self.b.ask(6, bytes([0, 1, 0]), 10)
        return got[2][0] == 255 and bad == 1, f'время вышло, пакет 8 с кодом 255; нулевое время - ответ {bad} (нужно 1)'

    def c_временные_id(self):
        _, got = self.b.ask(10, b'', 15, want=9)
        return bool(got and got[1] == 9), f'пакет 9: {len(self.b.temp_ids)} ID за сеанс' if got else 'нет пакета 9'

    def c_журнал(self):
        b = self.b
        if self.expected:
            self.say('ждём 35 с: конец синхронизации пишется через 30 с тишины' if any(e in range(6, 11) for _, e in self.expected) else 'забираем журнал')
            if any(e in range(6, 11) for _, e in self.expected):
                time.sleep(35)
        if not b.pull_journal():
            return False, b.journal_error
        got = b.events_since(self.t0)
        missing = [(c, e) for c, e in self.expected if e not in got]
        if b.journal:
            unknown = sorted({r.type for r in b.journal if r.type not in EVENTS})
        else:
            unknown = []
        text = f'записей за прогон {len(got)}, ожидалось {len(self.expected)} событий'
        if missing:
            text += '; НЕТ: ' + ', '.join(f'{e} {EVENTS.get(e)} ({c})' for c, e in missing)
        if unknown:
            text += f'; неизвестные номера: {unknown}'
        return not missing and not unknown, text

    # --- с галочкой ---
    def c_праздники(self):
        dates = [1767225600, 1767312000, 1778198400]
        results = []
        for values in (dates, dates[:1]):
            stamp = int(time.time())
            raw = struct.pack('<' + 'I' * len(values), *values)
            expected = crc16(raw.ljust(1024, b'\xff'))
            _, got = self.b.ask(16, struct.pack('<IB', stamp, len(values)) + raw, 20, want=12)
            ts_ok = bool(got and got[1] == 12 and got[2] == struct.pack('<BIH', 5, stamp, expected))
            _, crcs = self.b.ask(17, timeout=20, want=5)
            actual = struct.unpack_from('<H', crcs[2], 8)[0] if crcs and crcs[1] == 5 else None
            results.append(ts_ok and actual == expected)
            if not results[-1]:
                return False, f'список из {len(values)} дат: CRC {actual}, ожидалось {expected}; проверьте замену списка и хвост 0xFF'
        self.expect('праздники', 10)
        return all(results), 'дата с младшим байтом 0 принята; CRC списков 3→1 совпали'

    def c_синхронизация(self):
        now = int(time.time())
        res, ok = [], True
        cfg = self.opts.get('config')
        if cfg:
            data = bytearray(Path(cfg).read_bytes()[:512])
            struct.pack_into('<I', data, 0, now)
            code, got = self.b.ask(12, bytes(data), 20, want=12)
            ok &= bool(got and got[1] == 12 and got[2][0] == 4)
            res.append(f'настройки (из {Path(cfg).name}): {"таймштамп" if got and got[1] == 12 else code}')
            self.expect('синхронизация', 7)
        else:
            res.append('настройки пропущены: нет файла настроек платы (read_config.py <файл>)')
        hol = struct.pack('<IB', now, 1) + struct.pack('<I', int(datetime.datetime(datetime.date.today().year, 1, 1,
                                                                                    tzinfo=datetime.timezone.utc).timestamp()))
        sched = struct.pack('<IB', now, 1) + bytes([1, 0, 0, 23, 59, 0, 1, 1, 1, 1, 1, 1, 1] + [0] * 8 + [1] + [0] * 10)
        group = struct.pack('<IB', now, 1) + bytes([1, 0, 0, 1, 1, 1, 1, 1, 1, 1] + [0] * 6)
        for cmd, body, kind, ev, name in ((16, hol, 5, 10, 'праздники'), (14, sched, 3, 9, 'расписания'),
                                          (15, group, 2, 8, 'группы')):
            code, got = self.b.ask(cmd, body, 20, want=12)
            good = bool(got and got[1] == 12 and got[2][0] == kind and struct.unpack_from('<I', got[2], 1)[0] == now)
            ok &= good
            res.append(f'{name}: {"таймштамп верный" if good else code}')
            self.expect('синхронизация', ev)
        code, got = self.b.ask(13, self.id_cell(1, '9990000001', 255, 255), 20, want=12)
        good = bool(got and got[1] == 12 and got[2][0] == 1)
        ok &= good
        res.append(f'ID в ячейку 1: {"таймштамп" if good else code}')
        self.expect('синхронизация', 6)
        return ok, '; '.join(res) + '. Таблицы платы перезаписаны тестовыми записями'

    @staticmethod
    def id_cell(cell, ident, per_day, left, ts=None):
        rec = struct.pack('<H', cell) + ident.encode('ascii')[:10].ljust(10, b'\0') + bytes([1]) + \
            struct.pack('<IH', 0, 0) + bytes([1, per_day, left]) + bytes(10)
        return struct.pack('<IB', ts or int(time.time()), 1) + rec

    def c_проходы(self):
        code, got = self.b.ask(13, self.id_cell(2, '9990000002', 2, 2), 20, want=12)
        if not got or got[1] != 12:
            return False, f'ID с лимитом не записался ({code})'
        # Первый проход новых суток восстанавливает остаток до 2 и списывает: 1; второй - 0 (событие 79);
        # третий - отказ (ответ 2).
        codes = []
        for _ in range(3):
            codes.append(self.answer(8, bytes([3, 1, 0]) + b'9990000002')[1])
            self.output_off()
        self.expect('проходы', 74, 131, 79)
        return codes == [0, 0, 2], f'три прохода ID с лимитом 2 в сутки: ответы {codes} (нужно [0, 0, 2])'

    def c_sms(self):
        phone = self.opts.get('phone', '')
        if len(phone) < 10:
            return False, 'не задан номер телефона (10 цифр)'
        text = 'DOMOVOI test server'
        ok, code = self.answer(11, phone[-10:].encode('ascii') + text.encode('ascii').ljust(50, b'\0'), timeout=60)
        self.expect('sms', 36)
        return ok, f'ответ {code}, SMS на {phone[-10:]}: «{text}»'

    def c_фото(self):
        addr = int(self.opts.get('camera', 1))
        before = set(self.b.completed_photos)
        ok, code = self.answer(7, bytes([7, addr]))
        if not ok:
            return False, f'ответ {code} (камеры нет или 485 не настроен)'
        end = time.perf_counter() + 90
        with self.b.lock:
            while time.perf_counter() < end and not self.b.completed_photos - before:
                self.b.lock.wait(min(.25, max(0, end-time.perf_counter())))
        self.expect('фото', 129)
        complete = bool(self.b.completed_photos - before)
        return complete, 'фото получено полностью' if complete else 'фото не получено целиком за 90 с; проверьте пакет 14 и смещения'

    def c_перезагрузка(self):
        return self.c_связь_после_перезагрузки()

    def c_связь_после_перезагрузки(self):
        b = self.b
        s = b.sessions
        t0 = time.perf_counter()
        ok, code = self.answer(2, bytes([0]))
        end = t0 + 60
        with b.lock:
            while time.perf_counter() < end and not (b.sessions > s and b.reg and b.last_registration >= t0):
                b.lock.wait(min(.25, max(0, end-time.perf_counter())))
        elapsed = b.last_registration - t0
        back = b.sessions > s and b.reg and 0 <= elapsed < 60
        self.expect('перезагрузка', 33, 14)
        return ok and bool(back), f'ответ {code}, регистрация через {elapsed:.2f} с' if back else \
            'плата не зарегистрировалась за 60 с; проверьте новое TCP-соединение и запуск модема'

    def c_обновление(self):
        b = self.b
        path = self.opts.get('firmware')
        if not path:
            return False, 'не выбран файл прошивки'
        b.image = Image(path)
        b.fw_blocks_sent.clear()
        s = b.sessions
        self.expect('обновление', 132, 135, 136)
        t0 = time.perf_counter()
        u = b.send(19, b.image.cmd19())
        accepted = b.wait(lambda uu, c, p: (uu == u and c == 11 and p[:2] == bytes([19, 0])) or
                          (c == 2 and struct.unpack_from('<H', p, 4)[0] & 0x800), 20, t0)
        if not accepted:
            return False, 'нет успешного ответа 11 или статуса с битом 11; проверьте принятие команды 19'
        end = t0 + float(self.opts.get('update_timeout', 600))
        progress = b.wait(lambda uu, c, p: c == 4 and (p[1] == 253 or p[0] == 100 and p[1] == 0),
                          max(0, end-time.perf_counter()), t0)
        if not progress or progress[2][1] == 253:
            return False, 'загрузка прекращена' if progress else 'нет завершения загрузки по пакету 4; проверьте запросы блоков 13'
        done_at = next(t for t, uu, c, p in b.inbox if t >= t0 and (uu, c, p) == progress)
        duration = done_at - t0
        with b.lock:
            while time.perf_counter() < end and not (b.sessions > s and b.reg):
                b.lock.wait(min(.25, max(0, end-time.perf_counter())))
        if b.sessions == s or not b.reg:
            return False, 'прошивка загружена, но новой регистрации нет; проверьте установку и перезапуск'
        clear = b.wait(lambda uu, c, p: c == 2 and not struct.unpack_from('<H', p, 4)[0] & 0x800,
                       30, b.connected_at)
        if not clear:
            return False, 'после перезапуска бит 11 не снят; дальнейшие проверки остановлены'
        code, got = b.ask(22, b'', 15, want=3)
        build = struct.unpack_from('<I', got[2], 8)[0] if got and got[1] == 3 else None
        if not b.pull_journal():
            return False, b.journal_error
        missing = sorted({132, 135, 136} - set(b.events_since(self.t0)))
        text = f'сборка {build}, в файле {b.image.stamp}; загрузка {duration:.2f} с, ' \
               f'{len(b.fw_blocks_sent)/max(duration, .001):.2f} блок/с'
        if missing:
            text += f'; нет событий {missing}; проверьте журнал обновления'
        if build != b.image.stamp:
            text += '; установлен другой образ: проверьте статус 2'
        return build == b.image.stamp and not missing, text

    def report(self, results):
        LOGS.mkdir(exist_ok=True)
        path = Path(self.opts.get('output') or LOGS / f'проверка_сервером_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.txt')
        r = self.b.reg or {}
        lines = [f'Проверка тестовым сервером {datetime.datetime.now():%d.%m.%Y %H:%M}, плата IMEI {r.get("imei", "?")}, '
                 f'железо {r.get("hw", "?")}', '']
        lines += [f'{"ГОДНО   " if ok else "НЕ ГОДНО"} {n:14} {t}' +
                  ('' if ok else f'. Проверить: {self.TITLES.get(n, "подключение и журнал обмена")}') for n, ok, t in results]
        good = sum(ok for _, ok, _ in results)
        lines += ['', f'Итого: {good} из {len(results)} годно']
        got = sorted(set(self.b.events_since(self.t0)))
        missing = sorted({e for _, e in self.expected} - set(got))
        lines += ['Пришли: ' + ', '.join(f'{e} {EVENTS.get(e, "?")}' for e in got),
                  'Не пришли: ' + (', '.join(f'{e} {EVENTS.get(e, "?")}' for e in missing) or 'нет')]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        if self.opts.get('json'):
            Path(self.opts['json']).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        self.say(f'отчёт: {path}', 'head')


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
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


class Control(threading.Thread):
    """127.0.0.1:15011: строка команды -> строки ответа и END."""
    daemon = True

    def __init__(self, server, opts, say):
        super().__init__()
        self.server, self.opts, self.say = server, opts, say

    def run(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 15011))
        s.listen(1)
        while True:
            c, _ = s.accept()
            with c:
                line = c.makefile(encoding='utf-8').readline().strip()
                try:
                    out = self.do(line)
                except Exception as e:      # noqa: BLE001
                    out = [f'ошибка: {e!r}']
                c.sendall(('\n'.join(out) + '\nEND\n').encode('utf-8'))

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
            return [f'{"ГОДНО" if ok else "НЕ ГОДНО"} {n}: {t}' for n, ok, t in Checks(b, self.say, self.opts).run(names)]
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
        return ['команды: run all | run <проверки> [phone=79..] [config=файл] [firmware=файл] | send <cmd> <hex> | journal [n] | state']


def gui(server, opts, log_q, say):
    import tkinter as tk
    from tkinter import filedialog, ttk
    root = tk.Tk()
    root.title('ДОМОВОЙ NEXT - тестовый сервер')
    root.geometry('1100x760')
    left = ttk.Frame(root, padding=8)
    left.pack(side='left', fill='y')
    ttk.Label(left, text='Проверки (без риска)', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
    vars_ = {}
    for n in Checks.SAFE:
        v = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text=f'{n}: {Checks.TITLES[n]}', variable=v).pack(anchor='w')
        vars_[n] = v
    ttk.Label(left, text='Меняют плату - по галочке', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(10, 0))
    for n in Checks.RISKY:
        v = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text=f'{n}: {Checks.TITLES[n]}', variable=v).pack(anchor='w')
        vars_[n] = v
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
        names = [n for n in names if n != 'журнал'] + (['журнал'] if vars_['журнал'].get() else [])
        threading.Thread(target=lambda: Checks(server.board, say, opts).run(names), daemon=True).start()
    ttk.Button(left, text='Проверить отмеченное', command=start).pack(fill='x', pady=10)
    ttk.Button(left, text='Забрать журнал', command=lambda: threading.Thread(target=server.board.pull_journal, daemon=True).start()).pack(fill='x')
    text = tk.Text(root, wrap='word', font=('Consolas', 10))
    text.pack(side='left', fill='both', expand=True)
    for tag, color in (('good', '#0a7d22'), ('bad', '#b3261e'), ('head', '#1d4ed8'), ('event', '#6b21a8')):
        text.tag_config(tag, foreground=color)

    def pump():
        while True:
            try:
                kind, line = log_q.get_nowait()
            except queue.Empty:
                break
            text.insert('end', f'{datetime.datetime.now():%H:%M:%S} {line}\n', kind)
            text.see('end')
        root.after(200, pump)
    pump()
    root.mainloop()


def main():
    global LOGS, PHOTOS
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=15010)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--run', nargs='+', help='один прогон: all — обычные, full — включая изменение платы')
    ap.add_argument('--wait-board', type=float, default=60)
    ap.add_argument('--firmware')
    ap.add_argument('--config')
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
    log_path = LOGS / f'тестовый_сервер_{datetime.date.today():%Y-%m-%d}.log'
    log_q, lock = queue.Queue(), threading.Lock()

    class Say:
        def __call__(self, line, kind='info'):
            with lock, log_path.open('a', encoding='utf-8') as f:
                f.write(f'{datetime.datetime.now():%H:%M:%S} {line}\n')
            if a.headless:
                print(f'{datetime.datetime.now():%H:%M:%S} {line}', flush=True)
            else:
                log_q.put((kind, line))
    say = Say()
    opts = {k: v for k, v in vars(a).items() if v is not None}
    server = Server(a.port, say, a.bind)
    server.start()
    server.ready.wait(5)
    if server.error or not server.ready.is_set():
        raise SystemExit(2)
    if a.run:
        try:
            b = server.board
            end = time.perf_counter() + a.wait_board
            with b.lock:
                while time.perf_counter() < end and not (b.reg and b.status):
                    b.lock.wait(min(.2, max(0, end-time.perf_counter())))
            names = Checks.SAFE + Checks.RISKY if a.run == ['full'] else Checks.SAFE if a.run == ['all'] else a.run
            results = Checks(b, say, opts).run(names)
            if not b.reg and a.json:
                Path(a.json).write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8')
            raise SystemExit(0 if results and all(ok for _, ok, _ in results) else 1)
        finally:
            server.stop()
    Control(server, opts, say).start()
    if a.headless:
        while True:
            time.sleep(3600)
    else:
        gui(server, opts, log_q, say)


if __name__ == '__main__':
    main()
