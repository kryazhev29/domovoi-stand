"""Протокол2 ДОМОВОЙ NEXT для тестового сервера: кадр, CRC, названия событий, запись журнала, образ прошивки."""
import datetime, struct
from pathlib import Path


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
    39: 'перезагрузка по питанию', 40: 'причина перезапуска (флаги RCC)',
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


def parse_answer11(payload, command=None):
    """Код, время, буфер: 7 Б сейчас; 8 Б со старым номером команды."""
    if len(payload) == 8:
        if command is not None and payload[0] != command:
            raise ValueError('answer11: foreign command')
        payload = payload[1:]
    elif len(payload) != 7:
        raise ValueError('answer11: invalid payload length')
    return struct.unpack('<BIH', payload)


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
