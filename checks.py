"""Проверки протокола для тестового сервера. Перечитываются на ходу (команда reload), связь с платой не рвётся."""
import datetime, json, struct, time
from pathlib import Path

import importlib, protocol
importlib.reload(protocol)  # reload проверок подхватывает и новые номера событий
from protocol import EVENTS, Image, crc16, text10

LOGS = Path(__file__).resolve().parents[1] / 'Логи'
OUT1 = 2          # бит выхода 1 в статусе 1
UPDATE = 0x800    # бит 11: идёт обновление
JOURNAL = 0x1000  # бит 12: в журнале есть новые записи


def flags(p):
    return struct.unpack_from('<H', p, 4)[0]


class Checks:
    """Проверки протокола. Каждая: (ok, текст). Проверка не трогает чужое без галочки."""
    SAFE = ['связь', 'время', 'статус2', 'crc', 'crc_блоки', 'неизвестная', 'триггеры', 'выходы', 'считыватель',
            'временные_id', 'журнал']
    RISKY = ['синхронизация', 'праздники', 'проходы', 'sms', 'фото', 'перезагрузка', 'обновление']
    HEAVY = ['тяжёлая_база', 'тяжёлые_crc', 'тяжёлые_открытия']  # только по имени, в run full не входят
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
        'перезагрузка': 'команда 2 - ответ, события 33 и 14, регистрация быстрее 60 с',
        'обновление': 'команда 19 - прошивка из файла, события 132, 135, 136',
        'тяжёлая_база': 'команда 13 - 3000 ID пакетами по 30, время на пакет',
        'тяжёлые_crc': 'команда 18 - CRC всех блоков ID при полной базе, 3 раза',
        'тяжёлые_открытия': 'команда 8 по последнему ID и по отсутствующему, по 10 раз',
        'праздники': 'команда 16: три даты, затем одна; замена списка и CRC',
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
                if n not in self.TITLES:
                    raise KeyError(f'нет такой проверки; есть: {", ".join(self.SAFE + self.RISKY)}')
                self.settled()
                ok, text = getattr(self, 'c_' + n)()
            except Exception as e:     # noqa: BLE001 - проверка не роняет сервер
                ok, text = False, f'исключение: {e!r}'
            results.append((n, ok, text))
            self.say(f'{"ГОДНО" if ok else "НЕ ГОДНО"}: {n} - {text}', 'good' if ok else 'bad')
        if 'журнал' in names:
            self.say('=== проверка «журнал»: все ожидаемые события этого прогона', 'head')
            if b.status and b.status[1] & UPDATE:
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

    # --- ожидания: только по пакетам платы, без подобранных пауз ---
    def settled(self, timeout=30):
        """Плата в обычном сеансе: после регистрации пришёл статус 1. Сразу после регистрации
        плата ещё отправляет своё, и ответ на команду, посланную в эту секунду, может не успеть уйти."""
        if not self.b.reg:
            return False
        return bool(self.b.wait(lambda u, c, p: c == 2, timeout, self.b.last_registration))

    def output_pulse(self, since, timeout=60):
        """Импульс выхода 1 после since: статус «включён», затем «выключен». Включённый импульс
        не перезапускается, поэтому следующая команда 8 - только после выключения."""
        on = self.b.wait(lambda u, c, p: c == 2 and flags(p) & OUT1, 20, since, stamped=True)
        start = on[0] if on else since
        return bool(self.b.wait(lambda u, c, p: c == 2 and not flags(p) & OUT1, timeout, start))

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
        for _ in range(4):
            code, got = self.b.ask(22, b'', 15, want=3)
            if not got or got[1] != 3:
                return False, f'нет пакета 3 (ответ {code})'
            bt, csq, bal, fw, build, boot = struct.unpack_from('<IBHBIB', got[2])
            if csq != 99:
                break
            # 99 - модем после старта ещё не измерил сигнал: спросить снова после следующего статуса 1.
            self.b.wait(lambda u, c, p: c == 2, 20)
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
        st = b.wait(lambda u, c, p: c == 2 and (flags(p) >> 5 & 1) == flip[2], 20, t0)
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
                reason = ('выход занят' if before and before[1] & OUT1 else
                          'реле не разрешено командам сервера: проверьте настройки' if before and t0-before[3] <= 20 else
                          'нет свежего статуса, причина отказа не определена')
            off = self.output_pulse(t0)
            allok &= ok and off
            res.append(f'источник {src}: {code}' + (f' ({reason})' if reason else '') +
                       ('' if off else ' — выход не выключился за 60 с'))
            if ok:
                self.expect('выходы', ev)
        ok, code = self.answer(8, bytes([3, 2, 0]), want_code=1)
        res.append(f'выход=2: {code} (нужно 1)')
        return allok and ok, '; '.join(res)

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
        # Конец синхронизации (6-10) плата пишет после 30 с тишины: ждать, пока статус 1 не скажет
        # «в журнале новые записи», а не фиксированную паузу.
        sync = any(e in range(6, 11) for _, e in self.expected)
        end = time.perf_counter() + (45 if sync else 0)
        if sync:
            self.say('ждём события конца синхронизации (плата пишет их после 30 с тишины)')
        while True:
            since = time.perf_counter()
            if not b.pull_journal():
                return False, b.journal_error
            got = b.events_since(self.t0)
            missing = [(c, e) for c, e in self.expected if e not in got]
            left = end - time.perf_counter()
            if not missing or left <= 0:
                break
            b.wait(lambda u, c, p: c == 2 and flags(p) & JOURNAL, left, since)
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
            t0 = time.perf_counter()
            code = self.answer(8, bytes([3, 1, 0]) + b'9990000002')[1]
            codes.append(code)
            if code == 0:
                self.output_pulse(t0)
        self.expect('проходы', 74, 131, 79)
        return codes == [0, 0, 2], f'три прохода ID с лимитом 2 в сутки: ответы {codes} (нужно [0, 0, 2])'

    # --- тяжёлые: одинаковая нагрузка для сравнения сборок ---
    HEAVY_CELLS, HEAVY_PACK = 3000, 30   # вся база 50 048 = ~4 ч: пакет 13 идёт ~9 с

    @staticmethod
    def heavy_id(cell):
        return '8%09d' % cell

    def c_тяжёлая_база(self):
        """Все ячейки ID одной синхронизацией: один таймштамп, пакеты по 30 записей."""
        ts, times, bad = int(time.time()), [], 0
        for first in range(1, self.HEAVY_CELLS + 1, self.HEAVY_PACK):
            cells = range(first, min(first + self.HEAVY_PACK, self.HEAVY_CELLS + 1))
            body = struct.pack('<IB', ts, len(cells)) + b''.join(
                struct.pack('<H', c) + self.heavy_id(c).encode('ascii') + bytes([1]) +
                struct.pack('<IH', 0, 0) + bytes([1, 255, 255]) + bytes(10) for c in cells)
            t0 = time.perf_counter()
            code, got = self.b.ask(13, body, 30, want=12)
            if not got or got[1] != 12:
                bad += 1
                if bad > 3:
                    return False, f'ячейка {first}: нет ответа 12 ({code}), отказов {bad}'
                continue
            times.append(time.perf_counter() - t0)
        times.sort()
        n = len(times)
        return bad == 0, (f'{self.HEAVY_CELLS} ID, пакетов {n}, всего {sum(times):.0f} с; на пакет: медиана '
                          f'{times[n // 2] * 1000:.0f} мс, 95% {times[int(n * .95)] * 1000:.0f} мс, макс {times[-1] * 1000:.0f} мс; отказов {bad}')

    def c_тяжёлые_crc(self):
        res, ok = [], True
        for _ in range(3):
            t0 = time.perf_counter()
            _, got = self.b.ask(18, bytes([0]), 60, want=6)
            good = bool(got and got[1] == 6 and len(got[2]) == 200)
            ok &= good
            res.append(f'{(time.perf_counter() - t0) * 1000:.0f} мс' if good else 'нет ответа 6')
        return ok, 'CRC всех блоков: ' + ', '.join(res)

    def c_тяжёлые_открытия(self):
        last, hit, miss = self.heavy_id(self.HEAVY_CELLS), [], []
        for _ in range(10):
            t0 = time.perf_counter()
            code = self.answer(8, bytes([3, 1, 0]) + last.encode('ascii'), timeout=20)[1]
            hit.append((code, time.perf_counter() - t0))
            if code == 0:
                self.output_pulse(t0)
        for _ in range(10):
            t0 = time.perf_counter()
            code = self.answer(8, bytes([3, 1, 0]) + b'7000000000', timeout=20)[1]
            miss.append((code, time.perf_counter() - t0))

        def show(xs):
            ms = sorted(t * 1000 for _, t in xs)
            return f'коды {sorted({c for c, _ in xs}, key=str)}, ответ: медиана {ms[len(ms) // 2]:.0f} мс, макс {ms[-1]:.0f} мс'
        return all(c == 0 for c, _ in hit), f'последний ID {last}: {show(hit)}; нет в базе: {show(miss)}'

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
        self.b.until(lambda: self.b.completed_photos - before, 90)
        self.expect('фото', 129)
        complete = bool(self.b.completed_photos - before)
        return complete, 'фото получено полностью' if complete else 'фото не получено целиком за 90 с; проверьте пакет 14 и смещения'

    def c_перезагрузка(self):
        b = self.b
        s = b.sessions
        t0 = time.perf_counter()
        ok, code = self.answer(2, bytes([0]))
        b.until(lambda: b.sessions > s and b.reg and b.last_registration >= t0, 60)
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
                          (c == 2 and flags(p) & UPDATE), 20, t0)
        if not accepted:
            return False, 'нет успешного ответа 11 или статуса с битом 11; проверьте принятие команды 19'
        end = t0 + float(self.opts.get('update_timeout', 600))
        progress = b.wait(lambda uu, c, p: c == 4 and (p[1] == 253 or p[0] == 100 and p[1] == 0),
                          max(0, end-time.perf_counter()), t0)
        if not progress or progress[2][1] == 253:
            return False, 'загрузка прекращена' if progress else 'нет завершения загрузки по пакету 4; проверьте запросы блоков 13'
        done_at = next(t for t, uu, c, p in b.inbox if t >= t0 and (uu, c, p) == progress)
        duration = done_at - t0
        # Сеансы считаем от конца загрузки: переподключение во время неё (звонок) - не установка.
        s = b.sessions
        b.until(lambda: b.sessions > s and b.reg, max(0, end - time.perf_counter()))
        if b.sessions == s or not b.reg:
            return False, 'прошивка загружена, но новой регистрации нет; проверьте установку и перезапуск'
        clear = b.wait(lambda uu, c, p: c == 2 and not flags(p) & UPDATE,
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
        logs = Path(self.opts.get('log_dir') or LOGS)
        logs.mkdir(parents=True, exist_ok=True)
        path = Path(self.opts.get('output') or logs / f'проверка_сервером_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.txt')
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
