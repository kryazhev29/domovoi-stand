"""Снимки платы без окна: какая камера сняла, почему, адреса камер и тексты ответов платы.

Камеру видно только по самому JPEG: в протоколе её нет (заголовок фото, пакет 14 и событие 129 одинаковые).
JPEG-камера C328 пишет файл без APP0 JFIF (FF D8 FF DB…), камера номеров Interceptor — с APP0 JFIF.
Номер снимка — время (unix, UTC) события-причины до секунды.
Без tkinter и Pillow: модуль берёт и сервер (строка «Снимок №N принят» в «Обмене»).
"""
import bisect
import datetime
import os
from pathlib import Path

from protocol import ANSWER, text10


PLATE_TYPE, JPEG_TYPE = 8, 9          # типы строк таблицы 485 в копии настроек
TYPE_NAMES = {PLATE_TYPE: '8 — камера номеров', JPEG_TYPE: '9 — фотокамера JPEG'}
CAMERAS = {'jpeg': 'JPEG-камера', 'plate': 'камера номеров', 'unknown': 'камера не определена'}
CAMERA_TABS = {'jpeg': 'JPEG-камера', 'plate': 'Камера номеров'}
# После чего плата снимает (прошивка: открытие выходов при проходе, тревога, команда 7).
# «Нет в базе» (65) и «заблокирован» (66) выходы не открывают — снимка у них нет.
# 78 — номер считан по команде 6: за ним «Считать номер и снять» шлёт 7.
CAUSES = {45: 'снимок по тревоге', 69: 'проход по телефону', 70: 'проход по RFID 485', 71: 'проход по Wiegand',
          72: 'проход по брелку', 73: 'проход по номеру', 74: 'проход: тест из админки', 75: 'проход по API',
          76: 'проход: голосовой помощник', 77: 'проход из приложения', 78: 'по команде (номер считан)',
          80: 'открытие по кнопке'}
PLATE_EVENTS = (73, 78)               # у них ID — номер машины; у камеры номеров они важнее
READ_WINDOW = 15                      # 78 опережает снимок на опрос, связь и очередь команды 7
NO_CAUSE = 'снимок по команде или причина неизвестна'
BY_SERVER = 'снимок по команде с сервера'
NO_COPY = 'Копия настроек не загружена: адрес {camera} не известен — загрузите копию во вкладке «Настройки» или введите адрес вручную'
NO_ROW = 'В таблице 485 нет строки «{row}»: добавьте её во вкладке «Настройки» или введите адрес вручную'
FROM_TABLE = 'из таблицы 485 (копия настроек)'
PLATE_DEFAULT = 'по умолчанию 48 (serialAddr камеры)'
SHOT_WAIT = 60                        # столько ждём файл после «съёмка начата»
SHOT_SLACK = 120                      # часы платы и сервера: номер снимка может отличаться на столько
AUTO_NOTE = ('Снимки забираются автоматически: плата сообщает «фото в очереди», «Нависет» забирает за несколько секунд '
             'и сохраняет в папку {folder}. После этого на плате снимка нет.')
PLATE_SOURCES = {73: 'проход по номеру', 65: 'номера нет в базе', 66: 'номер заблокирован', 78: 'считан по команде'}


def local_when(t):
    """Время платы (unix, UTC) по часам этого компьютера: во вкладках камер и журнала — местное."""
    return datetime.datetime.fromtimestamp(t).strftime('%d.%m %H:%M:%S') if t else '-'


def jpeg_header(data):
    """(камера, ширина, высота) по началу файла: APP0 JFIF — камера номеров, без APP-сегментов — JPEG-камера.
    Нет кадра SOF (обрыв, мусор) — 'unknown'. JFIF 640×480 — тоже 'unknown': так снимает JPEG-модуль OV, не наш Interceptor."""
    if data[:3] != b'\xff\xd8\xff':
        return 'unknown', 0, 0
    kind, width, height, pos = 'jpeg', 0, 0, 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        while pos + 1 < len(data) and data[pos + 1] == 0xFF:
            pos += 1                  # байты-заполнители перед маркером
        if pos + 4 > len(data):
            break
        marker, length = data[pos + 1], int.from_bytes(data[pos + 2:pos + 4], 'big')
        if marker == 0xE0 and data[pos + 4:pos + 9] == b'JFIF\0':
            kind = 'plate' if kind == 'jpeg' else kind
        elif 0xE0 <= marker <= 0xEF:
            kind = 'unknown'          # EXIF и прочие APP — не наши камеры
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = int.from_bytes(data[pos + 5:pos + 7], 'big'), int.from_bytes(data[pos + 7:pos + 9], 'big')
            break
        if length < 2:
            break
        pos += 2 + length
    if not width or kind == 'plate' and (width, height) == (640, 480):
        kind = 'unknown'
    return kind, width, height


_headers = {}


def photo_info(path, stat=None):
    """jpeg_header файла с кэшем по полному пути, размеру и времени изменения."""
    stat = stat or path.stat()
    key, version = str(path), (stat.st_size, stat.st_mtime_ns)
    cached = _headers.get(key)
    if not cached or cached[0] != version:
        if len(_headers) > 5000:
            _headers.clear()
        with open(path, 'rb') as stream:
            cached = _headers[key] = version, jpeg_header(stream.read(65536))
    return cached[1]


def camera_kind(path):
    """'jpeg' | 'plate' | 'unknown'."""
    return photo_info(Path(path))[0]


def cause_index(journal):
    """События-причины, отсортированные по времени, и их времена — для поиска делением пополам."""
    causes = sorted((r for r in journal if r.type in CAUSES), key=lambda r: r.time)
    return causes, [r.time for r in causes]


def shot_cause(number, journal, commands=(), kind='unknown', index=None):
    """(причина, ID, код события) снимка №N: событие журнала за [N−2; N] с, иначе команда 7 рядом, иначе неизвестно.
    У JPEG-камеры 78 не причина; у остальных 73/78 важнее, а 78 ищется и раньше — до N−15 с."""
    causes, times = index or cause_index(journal)
    near = [r for r in causes[bisect.bisect_left(times, number - READ_WINDOW):bisect.bisect_right(times, number)]
            if (number - r.time <= 2 or r.type == 78) and not (kind == 'jpeg' and r.type == 78)]
    if near:
        best = min(near, key=lambda r: (number - r.time > 2, kind != 'jpeg' and r.type not in PLATE_EVENTS, number - r.time))
        return CAUSES[best.type], text10(best.arg1) if best.type != 45 else '', best.type
    if any(number - 5 <= sent <= number + 2 for sent in commands):
        return BY_SERVER, '', None
    return NO_CAUSE, '', None


def list_shots(folder, imei, journal=(), commands=()):
    """Снимки платы из Фото_с_камеры, новые сверху: имя, номер, местное время, размер, камера, причина.
    Посторонние имена пропускаются; файл, который как раз заменяется, — до следующего обновления."""
    index = cause_index(journal)
    prefix, shots = f'{imei}_', []
    try:
        entries = list(os.scandir(folder)) if imei else []
    except OSError:
        entries = []
    for entry in entries:
        digits = entry.name[len(prefix):-4]
        if (not entry.name.startswith(prefix) or not entry.name.endswith('.jpg') or not digits.isascii()
                or not digits.isdecimal() or len(digits) > 10 or int(digits) > 0xFFFFFFFF):
            continue
        number = int(digits)
        try:
            stat = entry.stat()
            kind, width, height = photo_info(Path(entry.path), stat)
        except OSError:
            continue
        reason, ident, event = shot_cause(number, (), commands, kind, index)
        plate = ident if event in PLATE_EVENTS else ''
        cause = f'{reason} {ident}'.strip()
        shots.append(dict(path=Path(entry.path), name=entry.name, number=number, time=local_when(number), size=stat.st_size,
                          kind=kind, width=width, height=height, ident=ident, plate=plate, cause=cause,
                          reason=reason if plate else cause))
    return sorted(shots, key=lambda s: s['number'], reverse=True)


def table_addresses(raw, kind):
    """Адреса строк таблицы 485 с этим типом из копии настроек (512 Б)."""
    return [raw[179 + i * 2] for i in range(8) if len(raw) == 512 and raw[180 + i * 2] == kind and raw[179 + i * 2]]


def camera_of_address(raw, address):
    """Какая камера стоит по адресу в копии настроек: 'plate', 'jpeg' или None (не видно)."""
    for kind, name in ((PLATE_TYPE, 'plate'), (JPEG_TYPE, 'jpeg')):
        if address in table_addresses(raw, kind):
            return name
    return None


def camera_address(raw, kind, saved=None):
    """(адрес для поля, пояснение): таблица 485 важнее сохранённого; без неё JPEG — пусто (или введённый),
    камера номеров — 48."""
    found = table_addresses(raw, kind)
    if found:
        return str(found[0]), FROM_TABLE
    missing = (NO_COPY.format(camera='JPEG-камеры' if kind == JPEG_TYPE else 'камеры номеров') if len(raw) != 512
               else NO_ROW.format(row=TYPE_NAMES[kind]))
    if kind == JPEG_TYPE:
        return (str(saved) if saved else ''), missing
    if saved and saved != 48:
        return str(saved), f'Введён вручную. {missing}'
    return '48', PLATE_DEFAULT


def camera_state(details, raw, kind, journal=()):
    """Строка «Камера: …» по таблице 485 копии настроек и последнему статусу 2; без копии — что известно."""
    states = bytes.fromhex(details['peripherals']) if details and details.get('peripherals') else b''
    states = states if len(states) >= 8 else b''
    checked = f'проверено {local_when(details["time"])}' if states else ''
    seen = latest_plate(None, journal) if kind == PLATE_TYPE else None
    seen = f'номер от камеры {local_when(seen[0])}' if seen else ''
    if len(raw) != 512:   # что это за копия, сказано под полем адреса; здесь — что известно без неё
        answered = [str(i + 1) for i in range(8) if states and states[i]]
        lines = ('по статусу 2 отвечают строки 485: ' + ', '.join(answered) if answered else
                 'по статусу 2 устройства 485 не отвечают' if states else 'ответы строк 485 — «Обновить (статус 2)»')
        return 'Камера: без копии настроек не видно, какая строка 485 — камера\n' + ' · '.join(filter(None, (lines, seen, checked)))
    rows = [i for i in range(8) if raw[180 + i * 2] == kind]
    if not rows:
        return f'Камера: в таблице 485 нет строки «{TYPE_NAMES[kind]}» (вкладка «Настройки»)' + (f' · {seen}' if seen else '')
    if not states:
        return 'Камера: неизвестно — нажмите «Обновить (статус 2)»'
    row = rows[0]
    return ' · '.join(filter(None, (f'Камера: {"отвечает" if states[row] else "нет ответа"}', f'адрес {raw[179 + row * 2]}',
                                    f'строка {row + 1}', checked, seen)))


def latest_plate(reader, journal):
    """(время, номер, откуда) самого свежего номера: пакет 8 и события 73/65/66/78; телефоны и ID без букв — не номер."""
    best = (reader[0], text10(reader[2]), 'считан по команде') if reader and reader[1] == 1 else None
    for record in reversed(journal):
        plate = text10(record.arg1) if record.type in PLATE_SOURCES else ''
        if any(c.isalpha() for c in plate):
            if not best or record.time > best[0] + 5:   # 78 того же захвата — не новее пакета 8
                best = record.time, plate, PLATE_SOURCES[record.type]
            break
    return best


def answer_text(cmd, code, camera, address):
    """Что значит ответ 11 платы на команду 7 (снимок) или 6 (захват номера) — по прошивке (photo_start, command_args_valid)."""
    if code == 0:
        return {7: 'Съёмка начата, ждём снимок…', 6: 'Камера ищет номер (до 15 с)…'}.get(cmd, 'Выполнено')
    if code == 1 and cmd == 7:
        return (f'Плата: камеры с адресом {address} нет в таблице 485, или «Режим RS-485» не «Активный опрос», '
                'или включён Wi-Fi-модем')
    if code == 1 and cmd == 6:
        return 'Плата отвергла параметры захвата (1)'
    if code == 2 and cmd == 7:
        return ('Камера номеров не видела номер последние 5 с (или занята память, или на плате 20 неотданных снимков, '
                'или плата на связи через Wi-Fi-мост)' if camera == 'plate' else
                'Камера или память заняты, или на плате 20 неотданных снимков, или не установлены часы, '
                'или плата на связи через Wi-Fi-мост')
    return f'Плата: {ANSWER.get(code, "код")} ({code})'


def reader_text(code, raw, address=None):
    """Пакет 8 после захвата 6: 1 — номер считан, 255 — время вышло (так же при неверном адресе)."""
    if code == 1:
        return f'Номер: {text10(raw)}'
    camera = f'камера с адресом {address}' if address else 'камера'
    return f'Время вышло: номер за 15 с не пришёл — номера нет в кадре, или {camera} не опрашивается (см. «Камера: …»)'


def shot_failed(camera):
    if camera == 'plate':
        return ('Снимок не получился за 60 с: камера номеров не отдала кадр (она снимает, только пока видит номер) '
                'или передача оборвалась — подробности в «Обмене»')
    return 'Снимок не получился за 60 с: камера не отдала кадр или передача оборвалась — подробности в «Обмене»'


def shot_done(shot, camera):
    """«Снимок получен»: если сняла другая камера — где его искать."""
    if shot['kind'] in (camera, 'unknown'):
        return f'Снимок получен: {shot["name"]}'
    return f'Снимок получен: {shot["name"]} — снимала {CAMERAS[shot["kind"]]}, он на вкладке «{CAMERA_TABS[shot["kind"]]}»'
