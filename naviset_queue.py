"""Очередь адаптера по Протокол2, строки 72, 86–97, 110–132.

SQLite хранит очередь по IMEI и исходы. Отправленную команду с неизвестным
результатом не повторяем автоматически: прошивка не гарантирует exactly-once.
Все методы вызываются под Board.lock. Часы подменяются только в тестах.
"""
import math
import sqlite3
import struct
import time
from pathlib import Path

from protocol import parse_answer11


def answer_code(payload, command=None):
    """Код ответа 11 или None, если кадр не ответ на эту команду."""
    try:
        return parse_answer11(payload, command)[0]
    except (ValueError, struct.error):
        return None

TTL = {2: 30, 3: 30, 5: 30, 6: 5, 7: 5, 8: 5, 9: 30, 10: 30,
       11: 60, 12: 120, 13: 120, 14: 120, 15: 120, 16: 120,
       17: 10, 18: 10, 19: 5, 20: 10, 21: 30, 22: 15, 23: 15,
       25: 30, 27: 30}
HIGH = {2, 3, 5, 8, 17, 18, 19, 23, 24}
SYNC = {12, 13, 14, 15, 16}
SYNC_KIND = {12: 4, 13: 1, 14: 3, 15: 2, 16: 5}
SYNC_BLOCKED = {2, 3, 6, 7, 9, 10, 11, 17, 18, 19, 20, 22, 23, 25, 27}


def priority(command):
    if command == 8:
        return 0
    if command in HIGH:
        return 1
    if command == 20:
        return 2
    return 3 if command in SYNC else 4


class CommandQueue:
    def __init__(self, path=':memory:', clock=time.time):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY, imei TEXT NOT NULL, uid INTEGER NOT NULL,
                cmd INTEGER NOT NULL, payload BLOB NOT NULL, priority INTEGER NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, ready REAL NOT NULL,
                sent REAL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                request_key TEXT);
            CREATE INDEX IF NOT EXISTS commands_peer ON commands(imei, state);
            CREATE TABLE IF NOT EXISTS journal (
                imei TEXT NOT NULL, raw BLOB NOT NULL, PRIMARY KEY(imei, raw));
        ''')
        self.clock = clock
        self.imei = None
        self.free = 0
        self.flags = 0
        self.online = False
        self.last_uid = 0

    def bind(self, imei):
        self.imei = imei
        self.online = True
        self.free = self.flags = 0
        # Запись sent выполняется ДО sendall: при падении исход неопределён,
        # повтор реле/SMS/сброса без нового поручения недопустим.
        self.db.execute("UPDATE commands SET state='unknown', reason='соединение прервано' "
                        "WHERE imei=? AND state='sent'", (imei,))
        self.db.commit()
        self.expire()

    def rows(self, states=('queued', 'sent')):
        marks = ','.join('?' for _ in states)
        return list(self.db.execute(f'SELECT * FROM commands WHERE imei=? AND state IN ({marks}) '
                                    'ORDER BY priority,id', (self.imei, *states)))

    def finish(self, row, state='done', reason=''):
        self.db.execute('UPDATE commands SET state=?,reason=? WHERE id=?', (state, reason, row['id']))
        self.db.commit()

    def expire(self):
        expired = [r for r in self.rows() if r['expires'] <= self.clock()]
        for row in expired:
            if row['state'] == 'sent':
                self.finish(row, 'unknown', 'нет ответа до истечения TTL; выполнение не подтверждено')
            else:
                self.finish(row, 'expired', 'TTL истёк до отправки')
        return expired

    def disconnect(self):
        self.online = False
        for row in self.rows(('sent',)):
            self.finish(row, 'unknown', 'связь прервана; выполнение не подтверждено')

    def enqueue(self, cmd, payload=b'', ttl=None, delay=0, request_key=None):
        if self.imei is None:
            raise RuntimeError('сначала требуется регистрация платы')
        ttl = TTL.get(cmd, 30) if ttl is None else ttl
        if not math.isfinite(ttl) or not math.isfinite(delay) or not 1 <= ttl <= 255 or delay < 0:
            raise ValueError('TTL: 1–255 секунд; задержка неотрицательная')
        if len(payload) > 2037:
            raise ValueError('кадр превышает 2048 байт')
        self.expire()
        now = self.clock()
        if request_key is not None:
            old = self.db.execute('SELECT * FROM commands WHERE imei=? AND request_key=? '
                                  'ORDER BY id DESC LIMIT 1', (self.imei, request_key)).fetchone()
            if old:
                if old['cmd'] != cmd or bytes(old['payload']) != bytes(payload):
                    raise ValueError('request_key уже использован для другой команды')
                return old['uid']
        # Дедупликация только одинакового незавершённого поручения. После ответа
        # следующая команда 9/10 является новым подтверждением, её не подавляем.
        for row in self.rows():
            if row['cmd'] == cmd and bytes(row['payload']) == bytes(payload):
                return row['uid']
        if cmd == 3:
            for row in self.rows():
                self.finish(row, 'cancelled', 'сброс на заводские: очередь очищена')
        # Не переиспользуем uniq до истечения TTL и окна поздних ответов.
        used = {r[0] for r in self.db.execute('SELECT uid FROM commands WHERE imei=? AND expires>?',
                                             (self.imei, now - 30))}
        for _ in range(256):
            self.last_uid = (self.last_uid + 1) & 255
            if self.last_uid not in used:
                break
        else:
            raise RuntimeError('все 256 uniq заняты; дождитесь TTL')
        self.db.execute('INSERT INTO commands(imei,uid,cmd,payload,priority,created,expires,ready,state,request_key) '
                        "VALUES(?,?,?,?,?,?,?,?,'queued',?)",
                        (self.imei, self.last_uid, cmd, bytes(payload), priority(cmd), now,
                         now + ttl, now + delay, request_key))
        self.db.commit()
        return self.last_uid

    def observe(self, uid, cmd, payload):
        self.expire()
        if cmd == 2:
            _, self.flags, self.free = struct.unpack_from('<IHH', payload)
        elif cmd == 11 and len(payload) in (7, 8):
            self.free = struct.unpack_from('<H', payload, len(payload) - 2)[0]
        # Запрос нового/повторного блока заменяет прежний ответ 20. Не ждём
        # потерянного пакета 4: подтверждено перепиской 23.09, 14:43.
        if cmd == 13:
            for row in self.rows():
                if row['cmd'] == 20:
                    self.finish(row, 'superseded', 'новый запрос блока 13')
            self.flags |= 0x800
        if cmd == 14 and payload and payload[0] == 0:
            # Пакеты фото идут потоком со своими uniq. Ответ 11 означает
            # приём запроса, нулевой пакет 14 — окончание потока.
            for row in self.rows(('sent',)):
                if row['cmd'] == 21:
                    self.finish(row, 'done', 'поток фото завершён')
        for row in self.rows(('sent',)):
            if row['uid'] != uid:
                continue
            original = row['cmd']
            # Ответ 11 по Протоколу2: код, время, буфер; команда известна по uniq (старый формат с номером тоже).
            answer = answer_code(payload, original) if cmd == 11 else None
            match = answer is not None
            if original == 21 and answer == 0:
                match = False
            match |= (original, cmd) in {(9, 10), (10, 9), (17, 5), (20, 4), (22, 3)}
            if original == 21:
                match |= cmd == 14 and bool(payload) and payload[0] == 0
            if original == 18:
                index = row['payload'][0] if row['payload'] else -1
                match |= cmd == 6 and index == 0 or cmd == 15 and payload[0] == index
            if original in SYNC and cmd == 12 and len(payload) >= 7:
                match |= payload[0] == SYNC_KIND[original] and payload[1:5] == row['payload'][:4]
            if match:
                code = answer if cmd == 11 else payload[1] if cmd == 4 else 0
                self.finish(row, 'done' if code == 0 else 'error', f'ответ {cmd}, код {code}')

    def flush(self, write):
        self.expire()
        if not self.online:
            return
        while True:
            rows = self.rows()
            sent = [r for r in rows if r['state'] == 'sent']
            sync_active = any(r['cmd'] in SYNC for r in sent)
            fw_active = bool(self.flags & 0x800) or any(r['cmd'] == 19 for r in sent)
            credit = max(0, self.free - sum(len(r['payload']) + 11 for r in sent))
            candidate = None
            for row in rows:
                cmd = row['cmd']
                if row['state'] != 'queued' or row['ready'] > self.clock():
                    continue
                if fw_active and (row['priority'] >= 3 or cmd == 8):
                    continue
                if sync_active and (cmd in SYNC_BLOCKED or cmd in SYNC):
                    continue
                # Нижняя очередь ждёт окончания верхних, а не только их отправки.
                if row['priority'] == 4 and any(r['priority'] < 4 for r in rows):
                    continue
                if len(row['payload']) + 11 > credit:
                    continue
                candidate = row
                break
            if candidate is None:
                return
            now = self.clock()
            ttl = max(1, min(255, math.ceil(candidate['expires'] - now)))
            self.db.execute("UPDATE commands SET state='sent',sent=? WHERE id=?", (now, candidate['id']))
            self.db.commit()
            write(candidate['cmd'], bytes(candidate['payload']), candidate['uid'], ttl)

    def latest(self, uid, cmd):
        """Последняя строка поручения с этим uniq и командой (для строки результата во вкладке)."""
        return self.db.execute('SELECT * FROM commands WHERE imei=? AND uid=? AND cmd=? ORDER BY id DESC LIMIT 1',
                               (self.imei, uid, cmd)).fetchone()

    def history(self, limit=150):
        return list(self.db.execute('SELECT * FROM commands WHERE imei=? ORDER BY id DESC LIMIT ?',
                                    (self.imei, limit)))[::-1]

    def state(self):
        self.expire()
        return {'imei': self.imei, 'online': self.online, 'free_reported': self.free,
                'reserved': sum(len(r['payload']) + 11 for r in self.rows(('sent',))),
                'commands': [{k: r[k] for k in ('id', 'uid', 'cmd', 'priority', 'state', 'expires', 'reason')}
                             for r in self.rows(('queued', 'sent', 'expired', 'error', 'unknown'))][-100:]}

    def store_records(self, records):
        fresh = []
        with self.db:
            for raw in records:
                if self.db.execute('INSERT OR IGNORE INTO journal VALUES(?,?)', (self.imei, raw)).rowcount:
                    fresh.append(raw)
        return fresh
