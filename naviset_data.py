"""Редактируемая серверная копия данных контроллеров; кодирование Протокол2."""
import copy
import datetime as dt
import json
import struct
import threading
import time
from pathlib import Path

from test_server import crc16


def integer(value, low, high, label):
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f'{label}: допустимо {low}–{high}')
    return number


def date_seconds(value):
    return int(dt.datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp()) if value else 0


# Камера номеров отдаёт номер латиницей, заглавными, добитым нулями до 10 Б, а плата сравнивает побайтно:
# русские буквы номера (их латинские двойники) переводятся, остальное должно быть латиницей и цифрами.
PLATE_LETTERS = str.maketrans('АВЕКМНОРСТУХавекмнорстух', 'ABEKMHOPCTYXABEKMHOPCTYX')


def plate_latin(text):
    plate = text.strip().translate(PLATE_LETTERS).upper()
    if not 1 <= len(plate) <= 10 or not plate.isascii() or not plate.isalnum():
        raise ValueError('Автономер: до 10 латинских букв и цифр без пробелов (русские АВЕКМНОРСТУХ переводятся сами)')
    return plate


def id_record(row):
    kind = integer(row['kind'], 1, 6, 'Тип ID')
    identifier = (plate_latin(row['identifier']) if kind == 3 else row['identifier']).encode('cp1251')
    if not 1 <= len(identifier) <= 10 or b'\0' in identifier:
        raise ValueError('ID: от 1 до 10 символов Windows-1251')
    daily = integer(row['daily'], 1, 255, 'Лимит проходов')
    if 251 <= daily <= 254:
        raise ValueError('Лимит: 1–250 или 255 (без ограничения)')
    remaining = integer(row['remaining'], 0, 255, 'Остаток проходов')
    if daily != 255 and remaining > daily:
        raise ValueError('Остаток превышает суточный лимит')
    expiry = 1 if row.get('deleted') else date_seconds(row.get('expiry', ''))
    return (struct.pack('<H', integer(row['cell'], 1, 50000, 'Ячейка')) + identifier.ljust(10, b'\0') +
            struct.pack('<BIHBBB', integer(row['group'], 1, 64, 'Группа'), expiry,
                        integer(row['minutes'], 0, 65535, 'Минуты'), 1 << (kind - 1), daily, remaining) + bytes(10))


def group_record(row):
    flags = [integer(row[k], 0, 1, k) for k in
             ('blocked', 'phone', 'rf433', 'rfid', 'wiegand', 'car', 'link', 'pin')]
    return bytes([integer(row['cell'], 1, 64, 'Группа'), integer(row['schedule'], 0, 64, 'Расписание'),
                  *flags]) + bytes(6)


def schedule_record(row):
    start = dt.datetime.strptime(row['start'], '%H:%M')
    end = dt.datetime.strptime(row['end'], '%H:%M')
    days = row['days']
    if len(days) != 7 or any(v not in '01' for v in days):
        raise ValueError('Дни: семь цифр 0/1, от понедельника до воскресенья')
    return bytes([integer(row['cell'], 1, 64, 'Расписание'), start.hour, start.minute, end.hour, end.minute,
                  integer(row['blocked'], 0, 1, 'Блокировка'), *(int(v) for v in days)]) + bytes(3)


def payloads(kind, data, stamp=None):
    stamp = int(time.time()) if stamp is None else stamp
    prefix = struct.pack('<I', stamp)
    if kind == 'ids':
        records = [id_record(r) for r in data.get(kind, [])]
        if len({r['cell'] for r in data.get(kind, [])}) != len(records):
            raise ValueError('повтор номера ячейки ID')
        return 13, [prefix + bytes([len(records[i:i + 32])]) + b''.join(records[i:i + 32])
                    for i in range(0, len(records), 32)]
    if kind == 'groups':
        source = {integer(r['cell'], 1, 64, 'Группа'): group_record(r) for r in data.get(kind, [])}
        # Полный снимок: отсутствующие группы заблокированы, старого хвоста нет.
        records = [source.get(i, bytes([i, 0, 1]) + bytes(13)) for i in range(1, 65)]
        return 15, [prefix + bytes([64]) + b''.join(records)]
    if kind == 'schedules':
        rows = data.get(kind, [])
        if not 1 <= len(rows) <= 64 or len({int(r['cell']) for r in rows}) != len(rows):
            raise ValueError('расписания: от 1 до 64 уникальных ячеек; пустой список прошивка не принимает')
        scenes = [integer(v.strip(), 0, 64, 'Сцена') for v in data.get('scenes', '0,0,0,0,0,0,0,0').split(',')]
        if len(scenes) != 8:
            raise ValueError('нужно 8 номеров сцен через запятую')
        tail = bytes(scenes + [integer(data.get('holiday_mode', 1), 1, 3, 'Праздники')]) + bytes(7)
        return 14, [prefix + bytes([len(rows)]) + b''.join(schedule_record(r) for r in rows) + tail]
    if kind == 'holidays':
        days = sorted({r['date'] for r in data.get(kind, [])})
        current_year = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).year
        if not 1 <= len(days) <= 128 or any(dt.date.fromisoformat(d).year != current_year for d in days):
            raise ValueError('от 1 до 128 праздников только текущего года; пустой список прошивка не принимает')
        return 16, [prefix + bytes([len(days)]) + b''.join(struct.pack('<I', date_seconds(d)) for d in days)]
    if kind == 'settings':
        original = bytes.fromhex(data.get('settings', ''))
        if len(original) != 512 or crc16(original[:510]) != int.from_bytes(original[510:], 'little'):
            raise ValueError('сначала загрузите корректную копию настроек, 512 байт с CRC')
        result = bytearray(original)
        result[:4] = prefix
        result[510:] = struct.pack('<H', crc16(result[:510]))
        return 12, [bytes(result)]
    raise ValueError('неизвестная структура')


class DataStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def get(self, imei):
        with self.lock:
            return copy.deepcopy(self.data.get(imei, {'name': imei, 'object': '', 'ids': [], 'groups': [],
                                                      'schedules': [], 'holidays': [], 'desired': {}}))

    def put(self, imei, data):
        if len(imei) != 15 or not imei.isascii() or not imei.isdigit():
            raise ValueError('IMEI: 15 цифр')
        with self.lock:
            updated = dict(self.data, **{imei: copy.deepcopy(data)})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)
            self.data = updated

    def prepare(self, imei, kind):
        with self.lock:
            data = self.get(imei)
            previous = data.get('desired', {}).get(kind, {}).get('stamp', 0)
            stamp = max(int(time.time()), previous + 1)
            cmd, frames = payloads(kind, data, stamp)
            if not frames:
                raise ValueError('нет ID для отправки; удаление задаётся пометкой конкретной записи')
            data.setdefault('desired', {})[kind] = dict(stamp=stamp, cmd=cmd, frames=[v.hex() for v in frames])
            self.put(imei, data)
            return cmd, frames
