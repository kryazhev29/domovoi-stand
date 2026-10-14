"""Сервер Нависет: диагностика платы, ручное управление, очереди по IMEI.

Запуск: python -X utf8 naviset_server.py [--adapter-profile profile.json]
Остальные параметры такие же, как у test_server.py. Сеть в тестах — localhost.
"""
import argparse
import datetime
import json
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import test_server as stand
from naviset_queue import CommandQueue, SYNC_KIND
from naviset_data import DataStore


DEFAULTS = dict(registration_timeout=5.0, heartbeat_timeout=30.0,
                command_delay=0.0, firmware_delay=0.0, firmware_ttl=10,
                drop_status_every=0, fragment_size=0, corrupt_block=-1,
                disconnect_after_blocks=0, stop_after_blocks=0,
                time_drift_limit=300, photo_retry=3.0, photo_stream_timeout=30.0,
                allowed_imeis=[], structures={})


def read_profile(path):
    values = json.loads(Path(path).read_text(encoding='utf-8')) if path else {}
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError('неизвестные параметры профиля: ' + ', '.join(sorted(unknown)))
    profile = dict(DEFAULTS, **values)
    for key in ('registration_timeout', 'heartbeat_timeout', 'photo_retry', 'photo_stream_timeout'):
        if not isinstance(profile[key], (int, float)) or not math.isfinite(profile[key]) or profile[key] <= 0:
            raise ValueError(f'{key} должен быть положительным числом')
    for key in ('command_delay', 'firmware_delay', 'time_drift_limit'):
        if not isinstance(profile[key], (int, float)) or not math.isfinite(profile[key]) or profile[key] < 0:
            raise ValueError(f'{key} должен быть неотрицательным числом')
    for key in ('drop_status_every', 'fragment_size', 'disconnect_after_blocks', 'stop_after_blocks'):
        if not isinstance(profile[key], int) or profile[key] < 0:
            raise ValueError(f'{key} должен быть неотрицательным целым')
    if not isinstance(profile['firmware_ttl'], int) or not 1 <= profile['firmware_ttl'] <= 255:
        raise ValueError('firmware_ttl: 1–255')
    if not isinstance(profile['corrupt_block'], int) or profile['corrupt_block'] < -1:
        raise ValueError('corrupt_block: -1 либо номер блока')
    if not isinstance(profile['allowed_imeis'], list) or any(
            not isinstance(v, str) or len(v) != 15 or not v.isascii() or not v.isdigit()
            for v in profile['allowed_imeis']):
        raise ValueError('allowed_imeis: список строк из 15 цифр')
    if not isinstance(profile['structures'], dict):
        raise ValueError('structures: объект с ключами IMEI')
    return profile


class Board(stand.Board):
    def __init__(self, say, profile=None, state_dir=None, data_store=None):
        super().__init__(say)
        self.profile = dict(DEFAULTS, **(profile or {}))
        self.state_dir = Path(state_dir or stand.LOGS / 'adapter')
        self.queue = None
        self.imei = None
        self.last_status = 0
        self.status_count = self.blocks_sent = 0
        self.corrupted = False
        self.photo_cursor = (0, 0)
        self.photo_last_complete = None
        self.photo_confirmed = False
        self.deferred_photo = None
        self.data_store = data_store
        self.details = None
        self.sync_plan = []
        self.photo_activity = 0.0
        self.photo_sizes = {}

    def trace(self, direction, raw, kind='frame'):
        now = datetime.datetime.now(datetime.timezone.utc)
        entry = dict(utc=now.isoformat(timespec='milliseconds'), monotonic=time.monotonic(),
                     imei=self.imei, session=self.sessions, direction=direction, kind=kind)
        if kind != 'decoded':
            entry['hex'] = raw.hex()
        if kind in ('frame', 'decoded') and len(raw) >= 11:
            entry.update(command=raw[8], uniq=raw[7], ttl=raw[6], payload=raw[9:-2].hex())
            if kind == 'decoded':
                del entry['ttl']
        if self.queue:
            entry.update(free=self.queue.free, reserved=sum(len(r['payload']) + 11 for r in self.queue.rows(('sent',))))
        folder = self.state_dir / 'traffic'
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f'{self.imei}_{now:%Y-%m-%d}.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def identify(self, imei):
        self.imei = imei
        self.details = None
        if self.queue is None:
            self.queue = CommandQueue(self.state_dir / f'{imei}.sqlite3')
        self.queue.bind(imei)
        self.sync_plan.clear()
        self.journal = [stand.Record(r[0]) for r in self.queue.db.execute(
            'SELECT raw FROM journal WHERE imei=? ORDER BY rowid DESC LIMIT 1500', (imei,))][::-1]
        self.last_status = time.monotonic()
        self.status_count = self.blocks_sent = 0
        if self.data_store:
            saved = self.data_store.get(imei)
            filename = saved.get('firmware_file')
            if filename:
                try:
                    image = stand.Image(filename)
                    if saved.get('firmware_header') == image.cmd19().hex():
                        self.image = image
                    else:
                        self.image = None
                        self.say(f'[{imei}] файл прошивки изменён или не был назначен: обновление не возобновлено', 'bad')
                except (OSError, ValueError):
                    self.say(f'[{imei}] сохранённый файл прошивки недоступен', 'bad')

    def wire(self, cmd, payload, uid, ttl=255):
        packet = stand.frame(cmd, payload, uid, ttl)
        fragment = self.profile['fragment_size'] or len(packet)
        for offset in range(0, len(packet), fragment):
            self.conn.sendall(packet[offset:offset + fragment])
        self.trace('TX', packet)
        if cmd == 20:
            block = int.from_bytes(payload[:2], 'little')
            self.fw_blocks_sent.add(block)
            self.blocks_sent += 1
        if cmd not in (1, 24):
            self.say(f'→ [{self.imei}] команда {cmd}, uniq={uid}, TTL={ttl}, {len(payload)} байт')
        if cmd == 5 and len(payload) == 16:
            self.last_triggers = bytes(payload)

    def send(self, cmd, payload=b'', ttl=None, request_key=None, delay=None):
        with self.lock:
            if self.queue is None:
                raise RuntimeError('плата ещё не зарегистрирована')
            if cmd in (1, 24):
                raise ValueError('ответы 1/24 формируются только на входящий запрос')
            if cmd == 3:
                self.sync_plan.clear()
            if cmd == 21:
                self.busy_photo = True
                self.photo_activity = time.monotonic()
            uid = self.queue.enqueue(cmd, payload, ttl,
                                     self.profile['command_delay'] if delay is None else delay,
                                     request_key)
            self.tick()
            return uid

    def tick(self):
        if self.queue is None:
            return
        for row in self.queue.expire():
            self.say(f'[{self.imei}] команда {row["cmd"]}, uniq={row["uid"]}: истёк TTL', 'bad')
            if row['cmd'] == 9:
                self.busy_journal = False
            elif row['cmd'] == 10:
                self.busy_ids = False
            elif row['cmd'] == 21:
                self.busy_photo = False
            if row['cmd'] in SYNC_KIND:
                self.sync_plan.clear()
        if not self.conn:
            return
        if time.monotonic() - self.last_status > self.profile['heartbeat_timeout']:
            raise ConnectionError('нет статуса 1 в пределах heartbeat_timeout')
        if self.deferred_photo and time.monotonic() >= self.deferred_photo[0]:
            _, payload = self.deferred_photo
            self.deferred_photo = None
            self.queue.enqueue(21, payload)
            self.photo_activity = time.monotonic()
        if self.busy_photo and time.monotonic() - self.photo_activity > self.profile['photo_stream_timeout']:
            self.queue.enqueue(21, struct.pack('<IH', *self.photo_cursor))
            self.photo_activity = time.monotonic()
        if self.sync_plan and not any(r['cmd'] in SYNC_KIND for r in self.queue.rows()):
            command, payload = self.sync_plan[0]
            try:
                self.queue.enqueue(command, payload)
            except RuntimeError as exc:
                if '256 uniq' not in str(exc):
                    raise
            else:
                self.sync_plan.pop(0)
        self.queue.flush(self.wire)
        limit = self.profile['disconnect_after_blocks']
        if limit and self.blocks_sent >= limit:
            raise ConnectionError('профиль: обрыв после блоков прошивки')

    def serve(self, conn, initial=b''):
        conn.settimeout(.1)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        buf = bytearray(initial)
        self.trace('RX', initial, 'chunk')
        try:
            while True:
                frames, buf = stand.take_frames(buf)
                with self.lock:
                    if self.conn is not conn:
                        return
                    for uid, cmd, payload in frames:
                        # Оригинальный TTL и границы TCP находятся в RX/chunk.
                        # Здесь отдельная удобная запись разобранного кадра.
                        self.trace('RX', stand.frame(cmd, payload, uid), 'decoded')
                        try:
                            self.handle(uid, cmd, payload)
                        except (ValueError, IndexError, struct.error) as exc:
                            self.say(f'[{self.imei}] повреждённый пакет {cmd}: {exc}', 'bad')
                            continue
                        self.inbox.append((time.perf_counter(), uid, cmd, payload))
                        del self.inbox[:-2000]
                        self.lock.notify_all()
                    self.tick()
                try:
                    data = conn.recv(4096)
                    if not data:
                        return
                    buf.extend(data)
                    with self.lock:
                        self.trace('RX', data, 'chunk')
                except socket.timeout:
                    pass
        finally:
            with self.lock:
                if self.conn is conn:
                    self.conn = None
                    if self.queue:
                        self.queue.disconnect()
                self.lock.notify_all()

    def handle(self, uid, cmd, payload):
        minimum = {1: 37, 2: 8, 3: 21, 4: 2, 5: 10, 6: 200, 8: 11,
                   9: 3, 10: 3, 11: 2, 12: 7, 13: 2, 14: 1, 15: 3}
        if len(payload) < minimum.get(cmd, 0):
            raise ValueError('недостаточная длина')
        if cmd in (9, 10) and len(payload) != 3 + payload[2] * (10 if cmd == 9 else 32):
            raise ValueError('число записей не соответствует длине')
        if cmd == 1:
            imei = payload[:15].decode('ascii')
            if imei != self.imei:
                raise ConnectionError('IMEI изменился внутри сеанса')
            stamps = struct.unpack_from('<5I', payload, 17)
            self.reg = dict(imei=imei, protocol=payload[15], hw=payload[16],
                            **dict(zip(('id', 'group', 'shedule', 'config', 'holiday'), stamps)))
            self.wire(1, bytes([0]) + struct.pack('<I', int(time.time())), uid, 5)
            self.last_registration = time.perf_counter()
            self.say(f'[{imei}] регистрация принята, HW {payload[16]}', 'good')
            self.sync_structures()
            return
        if self.queue is None or self.reg is None:
            raise ConnectionError('пакет до регистрации')
        self.queue.observe(uid, cmd, payload)
        if cmd == 11 and payload[0] in SYNC_KIND and payload[1]:
            self.sync_plan.clear()
            self.say(f'[{self.imei}] синхронизация остановлена: отказ команды {payload[0]}', 'bad')
        if cmd == 3:
            values = struct.unpack_from('<IBHBIB', payload)
            self.details = dict(time=values[0], signal=values[1], balance=values[2], firmware=values[3],
                                build=values[4], boot=values[5], peripherals=payload[13:21].hex(' '))
        if cmd == 2:
            bt, flags, free = struct.unpack_from('<IHH', payload)
            self.last_status = time.monotonic()
            self.status_count += 1
            self.status = (bt, flags, free, time.perf_counter())
            drop = self.profile['drop_status_every']
            if not drop or self.status_count % drop:
                self.wire(24, bytes([uid]), uid, 15)
            else:
                self.say(f'[{self.imei}] профиль: пропущен ответ 24')
            if abs(time.time() - bt) > self.profile['time_drift_limit']:
                if not any(r['cmd'] == 23 for r in self.queue.rows()):
                    self.send(23, struct.pack('<I', int(time.time())))
            if not flags & 0x800:
                for flag, command, busy in ((0x1000, 9, 'busy_journal'), (0x4000, 10, 'busy_ids')):
                    if self.auto_journal and flags & flag and not getattr(self, busy):
                        setattr(self, busy, True)
                        self.send(command)
                if self.auto_photo and flags & 0x2000 and not self.busy_photo:
                    self.busy_photo = True
                    self.photo_confirmed = False
                    self.send(21, struct.pack('<IH', *self.photo_cursor))
            return
        if cmd == 13:
            index = int.from_bytes(payload[:2], 'little')
            if self.profile['stop_after_blocks'] and self.blocks_sent >= self.profile['stop_after_blocks']:
                self.say(f'[{self.imei}] профиль: выдача прошивки остановлена')
                return
            if self.image is None or index >= self.image.blocks:
                self.say(f'[{self.imei}] блок {index}: образ не задан или номер вне образа', 'bad')
                return
            data = self.image.block(index)
            if index == self.profile['corrupt_block'] and not self.corrupted:
                data = data[:2] + bytes([data[2] ^ 1]) + data[3:]
                self.corrupted = True
            self.send(20, data, self.profile['firmware_ttl'], delay=self.profile['firmware_delay'])
            return
        if cmd == 10:
            raw = [payload[3 + i * 32:35 + i * 32] for i in range(payload[2])]
            records = [stand.Record(v) for v in self.queue.store_records(raw)]
            self.journal.extend(records)
            self.save_journal(records)
            for record in records:
                self.say(f'журнал [{self.imei}]: {record}', 'event')
            if raw:
                self.send(9)
            else:
                self.busy_journal = False
            return
        if cmd == 11 and payload[0] == 21 and payload[1] in (1, 2):
            if payload[1] == 1:
                self.photo_cursor = (0, 0)
                self.photo_last_complete = None
            self.deferred_photo = (time.monotonic() + self.profile['photo_retry'],
                                   struct.pack('<IH', *self.photo_cursor))
            self.photo_confirmed = False
        super().handle(uid, cmd, payload)

    def photo_packet(self, payload):
        self.photo_activity = time.monotonic()
        if payload[0] == 0:
            number, received = self.photo_cursor
            if number in self.photo_sizes and received < self.photo_sizes[number]:
                self.send(21, struct.pack('<IH', number, received))
                return
            if self.photo_last_complete and not self.photo_confirmed:
                self.photo_confirmed = True
                self.send(21, struct.pack('<IH', *self.photo_last_complete))
            else:
                if self.photo_confirmed:
                    # Пустой поток после ACK: старый курсор больше не нужен.
                    cursor = self.state_dir / 'photos' / self.imei / 'cursor.json'
                    cursor.unlink(missing_ok=True)
                    self.photo_cursor = (0, 0)
                    self.photo_last_complete = None
                    self.photo_confirmed = False
                self.busy_photo = False
            return
        if len(payload) < 11:
            raise ValueError('неполный заголовок фото')
        number, size, offset = struct.unpack_from('<IIH', payload, 1)
        chunk = payload[11:]
        if not 0 < size <= 65535 or not 0 < len(chunk) <= 1024 or offset + len(chunk) > size:
            raise ValueError('размер/смещение фото вне границ')
        folder = self.state_dir / 'photos' / self.imei
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{number}.part'
        meta = folder / f'{number}.size'
        if meta.exists() and int(meta.read_text()) != size:
            raise ValueError('размер того же фото изменился')
        meta.write_text(str(size))
        self.photo_sizes[number] = size
        self.photo_confirmed = False
        data = bytearray(path.read_bytes()) if path.exists() else bytearray()
        if offset > len(data):
            self.photo_cursor = (number, len(data))
            self.send(21, struct.pack('<IH', *self.photo_cursor))
            return
        overlap = min(len(chunk), len(data) - offset)
        if data[offset:offset + overlap] != chunk[:overlap]:
            raise ValueError('повтор блока фото отличается от сохранённых данных')
        data.extend(chunk[overlap:])
        with path.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.photos[number] = data
        self.photo_cursor = (number, len(data))
        if len(data) == size:
            stand.PHOTOS.mkdir(parents=True, exist_ok=True)
            final = stand.PHOTOS / f'{self.imei}_{number}.jpg'
            temporary = final.with_suffix('.tmp')
            with temporary.open('wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(final)
            self.completed_photos.add(number)
            self.photo_last_complete = (number, size)
        # Сохраняем курсор после данных, до подтверждения контроллеру.
        cursor = folder / 'cursor.json'
        tmp = folder / 'cursor.tmp'
        tmp.write_text(json.dumps(self.photo_cursor), encoding='utf-8')
        tmp.replace(cursor)

    def sync_structures(self):
        cursor = self.state_dir / 'photos' / self.imei / 'cursor.json'
        if cursor.exists():
            self.photo_cursor = tuple(json.loads(cursor.read_text(encoding='utf-8')))
        desired = self.profile['structures'].get(self.imei, {})
        if self.data_store:
            local = self.data_store.get(self.imei).get('desired', {})
            desired = dict(desired, **{str(v['cmd']): v['frames'] for v in local.values()})
        names = {12: 'config', 13: 'id', 14: 'shedule', 15: 'group', 16: 'holiday'}
        for key, frames in desired.items():
            command = int(key)
            if command not in SYNC_KIND or not isinstance(frames, list):
                raise ValueError('structures: команды 12–16, список hex-пакетов')
            decoded = [bytes.fromhex(v) for v in frames]
            if not decoded or any(len(v) < 4 for v in decoded):
                raise ValueError('structures: нет таймштампа')
            stamp = int.from_bytes(decoded[0][:4], 'little')
            if any(int.from_bytes(v[:4], 'little') != stamp for v in decoded):
                raise ValueError('разные таймштампы одной структуры')
            if self.reg[names[command]] <= stamp:
                # Снимок ID может содержать много пакетов с одной меткой. Равенство
                # меток после обрыва НЕ доказывает, что передан весь снимок.
                for payload in decoded:
                    done = self.queue.db.execute("SELECT 1 FROM commands WHERE imei=? AND cmd=? AND payload=? AND state='done'",
                                                 (self.imei, command, payload)).fetchone()
                    if self.reg[names[command]] < stamp or not done:
                        self.sync_plan.append((command, payload))


class Server(stand.Server):
    def __init__(self, port, say, host='0.0.0.0', profile=None, state_dir=None):
        super().__init__(port, say, host)
        self.profile = dict(DEFAULTS, **(profile or {}))
        self.state_dir = Path(state_dir or stand.LOGS / 'adapter')
        self.data_store = DataStore(self.state_dir / 'controllers.json')
        self.board = Board(say, self.profile, self.state_dir, self.data_store)
        self.boards = {}
        self.peers_lock = threading.Lock()
        self.sockets = set()
        self.workers = []

    def client(self, conn):
        try:
            deadline = time.monotonic() + self.profile['registration_timeout']
            conn.settimeout(.1)
            buf = bytearray()
            while time.monotonic() < deadline and not self.stopping:
                try:
                    part = conn.recv(4096)
                except socket.timeout:
                    continue
                if not part:
                    return
                buf.extend(part)
                frames, _ = stand.take_frames(buf)
                if not frames:
                    if len(buf) > 8192:
                        raise ConnectionError('мусор вместо регистрации')
                    continue
                uid, cmd, payload = frames[0]
                if cmd != 1 or len(payload) != 37 or not payload[:15].isdigit():
                    raise ConnectionError('первый пакет должен быть регистрацией с IMEI')
                imei = payload[:15].decode('ascii')
                allowed = self.profile['allowed_imeis']
                if allowed and imei not in allowed:
                    raise ConnectionError('IMEI отсутствует в allowed_imeis')
                with self.peers_lock:
                    if imei not in self.boards:
                        self.boards[imei] = self.board if not self.boards and not self.board.imei else Board(
                            self.say, self.profile, self.state_dir, self.data_store)
                    board = self.boards[imei]
                # bind/attach под одним замком: старый сеанс не меняет новую очередь.
                with board.lock:
                    board.attach(conn)
                    board.identify(imei)
                board.serve(conn, bytes(buf))
                return
            raise ConnectionError('регистрация не пришла за отведённое время')
        except (OSError, ValueError) as exc:
            if not self.stopping:
                self.say(f'сеанс закрыт: {exc}', 'bad')
        finally:
            with self.peers_lock:
                self.sockets.discard(conn)
            conn.close()

    def run(self):
        listener = socket.socket()
        self.listener = listener
        listener.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', socket.SO_REUSEADDR), 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(32)
            self.port = listener.getsockname()[1]
            listener.settimeout(.2)
            self.ready.set()
            self.say(f'адаптер Нависет: порт {self.port}; очереди, TTL, буфер; состояние {self.state_dir}')
            while not self.stopping:
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with self.peers_lock:
                    self.sockets.add(conn)
                worker = threading.Thread(target=self.client, args=(conn,), daemon=True)
                self.workers = [w for w in self.workers if w.is_alive()]
                self.workers.append(worker)
                worker.start()
        except OSError as exc:
            if not self.stopping:
                self.error = str(exc)
                self.say(f'ошибка сервера: {exc}', 'bad')
        finally:
            self.ready.set()
            listener.close()

    def stop(self):
        super().stop()
        with self.peers_lock:
            sockets = list(self.sockets)
        for conn in sockets:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        for worker in self.workers:
            if worker is not threading.current_thread():
                worker.join(2)
        for board in self.boards.values():
            with board.lock:
                if board.queue:
                    board.queue.disconnect()


class Control(threading.Thread):
    daemon = True

    def __init__(self, server, opts, say, show=None, port=15011):
        super().__init__()
        self.server, self.say, self.show = server, say, show
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', socket.SO_REUSEADDR), 1)
        try:
            self.sock.bind(('127.0.0.1', port))
            self.sock.listen(8)
        except OSError:
            self.sock.close()
            raise OSError(f'Порт управления {port} занят. Сначала закройте прежний тестовый сервер.')
        self.sock.settimeout(.2)

    def run(self):
        while not self.server.stopping:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.client, args=(conn,), daemon=True).start()

    def client(self, conn):
        with conn:
            conn.settimeout(20)
            try:
                line = conn.makefile(encoding='utf-8').readline(8192).strip()
                output = self.do(line)
            except Exception as exc:
                output = [f'ошибка: {exc}']
            try:
                conn.sendall(('\n'.join(output) + '\nEND\n').encode('utf-8'))
            except OSError:
                pass

    def do(self, line):
        parts = line.split()
        if parts and parts[0] == 'identity':
            return [json.dumps(dict(app='domovoi-naviset', port=self.server.port,
                                   host=self.server.host, state=str(self.server.state_dir.resolve())), ensure_ascii=False)]
        if parts and parts[0] in ('run', 'reload'):
            return ['Этот сервер предназначен для ручной работы с контроллерами.']
        if parts and parts[0] == 'peers':
            with self.server.peers_lock:
                return [f'{imei}: online={bool(b.conn)}, sessions={b.sessions}'
                        for imei, b in self.server.boards.items()]
        if parts and parts[0] == 'use':
            with self.server.peers_lock:
                selected = self.server.boards[parts[1]]
                if self.server.board.check_lock.locked():
                    raise RuntimeError('дождитесь окончания проверки выбранной платы')
                self.server.board = selected
            return [f'выбрана плата {parts[1]}']
        board = self.server.board
        if parts and parts[0] == 'show':
            if self.show:
                self.show()
            return ['окно сервера показано']
        if parts and parts[0] == 'queue':
            with board.lock:
                return [json.dumps(board.queue.state() if board.queue else {}, ensure_ascii=False)]
        if parts and parts[0] == 'enqueue':
            # Ключ поручения позволяет безопасно повторить HTTP/CLI-запрос оператора.
            # enqueue <key> <cmd> <ttl> [hex]
            uid = board.send(int(parts[2]), bytes.fromhex(''.join(parts[4:])),
                             ttl=int(parts[3]), request_key=parts[1])
            return [f'в очереди: uniq={uid}']
        if parts and parts[0] == 'state':
            return [f'режим: Нависет; IMEI {board.imei}; связь: {bool(board.conn)}; очередь: ctl.py queue']
        if parts and parts[0] == 'send':
            uid = board.send(int(parts[1]), bytes.fromhex(''.join(parts[2:])))
            return [f'в очереди: uniq={uid}']
        return ['команды: peers | use <IMEI> | state | queue | show | send <cmd> [hex] | enqueue <key> <cmd> <ttl> [hex]']


def show_existing(port, host, board_port, state_dir):
    """Открываем только совместимый экземпляр, а не старый test_server."""
    if not port:
        return False
    def request(command):
        with socket.create_connection(('127.0.0.1', port), timeout=1) as connection:
            connection.sendall((command + '\n').encode('utf-8'))
            with connection.makefile(encoding='utf-8') as stream:
                return stream.readline(8192).strip()
    try:
        identity = json.loads(request('identity'))
    except (OSError, ValueError):
        return False
    if not isinstance(identity, dict) or identity.get('app') != 'domovoi-naviset':
        return False
    if (identity.get('port') != board_port or identity.get('host') != host or
            Path(identity.get('state', '')).resolve() != Path(state_dir).resolve()):
        raise ValueError('Другой сервер Нависет уже использует порт управления. Проверьте параметры запуска.')
    request('show')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adapter-profile')
    parser.add_argument('--adapter-state')
    parser.add_argument('--port', type=int, default=15010)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--minimized', action='store_true')
    parser.add_argument('--log-dir')
    parser.add_argument('--control-port', type=int, default=15011)
    options = parser.parse_args()
    profile = read_profile(options.adapter_profile)
    if options.log_dir:
        stand.LOGS = Path(options.log_dir)
        stand.PHOTOS = stand.LOGS / 'photos'
    stand.LOGS.mkdir(parents=True, exist_ok=True)
    state_dir = options.adapter_state or stand.LOGS / 'adapter'
    if show_existing(options.control_port, options.bind, options.port, state_dir):
        return
    messages = queue.Queue()
    log_lock = threading.Lock()

    def say(text, kind='info'):
        now = datetime.datetime.now().astimezone()
        with log_lock, (stand.LOGS / f'адаптер_{now:%Y-%m-%d}.log').open('a', encoding='utf-8') as stream:
            stream.write(f'{now.isoformat(timespec="milliseconds")} [{kind}] {text}\n')
        if options.headless:
            print(text, flush=True)
        else:
            messages.put((kind, text))

    server = Server(options.port, say, options.bind, profile, options.adapter_state)
    # Не показываем старое окно и не выдаём старый сервер за новый режим.
    try:
        control = Control(server, {}, say, lambda: messages.put(('show', '')), options.control_port)
    except OSError:
        if show_existing(options.control_port, options.bind, options.port, state_dir):
            return
        raise
    server.start()
    server.ready.wait(5)
    if server.error or not server.ready.is_set():
        server.stop()
        raise SystemExit(f'Сервер не запущен: {server.error or "таймаут"}')
    control.start()
    threading.excepthook = lambda e: say(f'Ошибка потока: {e.exc_value}', 'bad')
    try:
        if options.headless:
            while not server.stopping:
                time.sleep(.5)
        else:
            from naviset_ui import App
            App(server, messages, say, options.minimized).run()
    finally:
        server.stop()
        control.sock.close()


if __name__ == '__main__':
    main()
