"""Диагностика связи без изменения настроек, выходов, SMS и прошивки."""
import datetime as dt
import json
import struct
import threading
import time
from pathlib import Path


CHECKS = (('connection', 'Связь с платой'), ('status', 'Версия и состояние'),
          ('clock', 'Время платы'), ('crc', 'Чтение CRC структур'))


class DiagnosticStopped(Exception):
    pass


class Diagnostics:
    def __init__(self, board, emit=lambda result: None, timeout=15):
        self.board, self.emit, self.timeout = board, emit, timeout
        self.cancel = threading.Event()
        self.session = board.sessions
        self.connection = board.conn
        self.rows = []
        self.started = dt.datetime.now().astimezone().isoformat(timespec='seconds')

    def guard(self):
        if self.cancel.is_set():
            raise DiagnosticStopped('Проверка остановлена оператором')
        if not self.board.conn or self.board.conn is not self.connection or self.board.sessions != self.session:
            raise DiagnosticStopped('Связь прервалась. Подключите плату и повторите проверку')

    def result(self, key, state, detail):
        item = dict(key=key, title=dict(CHECKS)[key], state=state, detail=detail)
        self.rows.append(item)
        self.emit(item)

    def request(self, command, response):
        board = self.board
        with board.lock:
            self.guard()
            since = time.perf_counter()
            uid = board.send(command, ttl=max(1, min(255, int(self.timeout))))
            row = next(r for r in board.queue.rows() if r['uid'] == uid and r['cmd'] == command)
            row_id = row['id']
        deadline = time.monotonic() + self.timeout
        try:
            with board.lock:
                while True:
                    self.guard()
                    for stamp, received_uid, cmd, data in board.inbox:
                        if stamp < since or received_uid != uid:
                            continue
                        if cmd == response:
                            return data
                        if cmd == 11 and len(data) >= 2 and data[0] == command and data[1]:
                            raise ValueError(f'Плата отклонила запрос: код {data[1]}')
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError(f'Нет ответа за {self.timeout:g} с. Проверьте связь и очередь')
                    board.lock.wait(min(left, .2))
        finally:
            with board.lock:
                # Остановленный прогон не оставляет запросов для будущего сеанса.
                row = board.queue.db.execute('SELECT * FROM commands WHERE id=?', (row_id,)).fetchone()
                if row and row['state'] == 'queued':
                    board.queue.finish(row, 'cancelled', 'диагностика завершена до отправки')

    def run(self):
        board = self.board
        if not board.check_lock.acquire(blocking=False):
            raise ValueError('Проверка этой платы уже выполняется')
        try:
            with board.lock:
                self.guard()
                if not board.reg or not board.status:
                    raise DiagnosticStopped('Дождитесь регистрации и первого статуса платы')
                if board.queue.flags & 0x800 or board.sync_plan or board.queue.rows():
                    raise DiagnosticStopped('Дождитесь окончания команд, синхронизации или обновления')
                age = time.perf_counter() - board.status[3]
                if age > board.profile['heartbeat_timeout']:
                    raise DiagnosticStopped('Статус устарел. Дождитесь нового подключения')
                hardware = board.reg['hw']
            self.result('connection', 'pass', f'Регистрация принята · HW {hardware} · сеанс {self.session}')
            status = None
            try:
                status = self.request(22, 3)
                if len(status) < 21:
                    raise ValueError('Неполный ответ статуса')
                stamp, signal, balance, firmware, build, boot = struct.unpack_from('<IBHBIB', status)
                signal_text = f'{signal}/31' if 0 <= signal <= 31 else 'не определён'
                self.result('status', 'pass', f'FW {firmware} · сборка {build} · загрузчик 0x{boot:02X} · сигнал {signal_text}')
            except (TimeoutError, ValueError) as exc:
                status = None
                self.result('status', 'fail', str(exc))
            if status is None:
                self.result('clock', 'skip', 'Нет свежего ответа статуса')
            else:
                delta = abs(time.time() - struct.unpack_from('<I', status)[0])
                limit = board.profile['time_drift_limit']
                self.result('clock', 'pass' if delta <= limit else 'warn',
                            f'Расхождение с компьютером {delta:.0f} с · допустимо {limit:g} с')
            try:
                data = self.request(17, 5)
                values = struct.unpack('<5H', data)
                detail = ' · '.join(f'{name}: {value:04X}' for name, value in
                                    zip(('ID', 'Группы', 'Расписания', 'Настройки', 'Праздники'), values))
                self.result('crc', 'pass', detail + '. CRC прочитаны; сравнение с эталоном не выполнялось')
            except (TimeoutError, ValueError, struct.error) as exc:
                self.result('crc', 'fail', str(exc))
            with board.lock:
                self.guard()
        except DiagnosticStopped as exc:
            finished = {row['key'] for row in self.rows}
            remaining = [key for key, _ in CHECKS if key not in finished]
            if remaining:
                for key in remaining:
                    self.result(key, 'skip', str(exc))
            else:
                self.result('connection', 'fail', str(exc))
        finally:
            board.check_lock.release()
        return self.report()

    def report(self):
        latest = {row['key']: row for row in self.rows}
        rows = [latest[key] for key, _ in CHECKS if key in latest]
        passed = len(rows) == len(CHECKS) and all(r['state'] == 'pass' for r in rows)
        return dict(imei=self.board.imei, session=self.session, started=self.started,
                    finished=dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                    passed=passed, results=rows,
                    scope='Область проверки: связь, ответы статуса, время и чтение CRC. '
                          'Реле, считыватели, камера, звонки и SMS требуют отдельных проверок.')


def save_report(report, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f'проверка_{report["imei"]}_{dt.datetime.now():%Y%m%d_%H%M%S_%f}'
    destination = folder / (stem + '.txt')
    labels = dict(pass_='Пройдено', fail='Ошибка', warn='Внимание', skip='Не проверено')
    lines = ['ДОМОВОЙ · проверка платы', f'IMEI: {report["imei"]}', f'Начало: {report["started"]}',
             f'Сеанс: {report["session"]}', '']
    for row in report['results']:
        label = labels['pass_' if row['state'] == 'pass' else row['state']]
        lines.append(f'{row["title"]}: {label}\n  {row["detail"]}')
    lines.extend(['', report['scope']])
    destination.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    destination.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return destination
