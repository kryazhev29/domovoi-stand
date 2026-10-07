"""Ручная работа оператора с контроллерами. Автотестов в интерфейсе нет."""
import csv
import datetime as dt
import json
import os
import queue
import sqlite3
import struct
import tempfile
import threading
import time
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import test_server as stand
from naviset_data import id_record, group_record, schedule_record, payloads, integer


KINDS = ['1 — Телефон', '2 — RFID 485', '3 — Автономер', '4 — RF433', '5 — Wiegand', '6 — ПИН']
SCHEMAS = {
    'ids': [('cell', 'Ячейка', '1'), ('identifier', 'ID абонента', ''), ('group', 'Группа', '1'),
            ('kind', 'Тип идентификатора', '1'), ('expiry', 'Запрет с даты (ГГГГ-ММ-ДД, пусто — бессрочно)', ''),
            ('minutes', 'Минут после первого прохода (0 — бессрочно)', '0'),
            ('daily', 'Проходов в сутки (255 — без ограничения)', '255'),
            ('remaining', 'Осталось проходов сегодня', '255')],
    'groups': [('cell', 'Группа', '1'), ('schedule', 'Расписание (0 — круглосуточно)', '0'),
               ('blocked', 'Заблокирована', '0'), ('phone', 'Звонки', '1'), ('rf433', 'RF433', '0'),
               ('rfid', 'RFID 485', '1'), ('wiegand', 'Wiegand', '1'), ('car', 'Автономер', '1'),
               ('link', 'Ссылка доступа', '1'), ('pin', 'ПИН', '1')],
    'schedules': [('cell', 'Расписание', '1'), ('start', 'Начало (ЧЧ:ММ)', '00:00'),
                  ('end', 'Конец (ЧЧ:ММ)', '23:59'), ('blocked', 'Заблокировано', '0'),
                  ('days', 'Дни недели', '1111111')],
    'holidays': [('date', 'Дата (ГГГГ-ММ-ДД)', dt.date.today().isoformat())],
}
TITLES = {'ids': 'Абоненты / ID', 'groups': 'Группы', 'schedules': 'Расписания', 'holidays': 'Праздники'}
STATES = {'queued': 'В очереди', 'sent': 'Отправлено', 'done': 'Выполнено', 'error': 'Отказ',
          'expired': 'Истёк срок', 'unknown': 'Результат неизвестен', 'cancelled': 'Отменено',
          'superseded': 'Заменено новым запросом'}
COMMANDS = {2: 'Перезагрузка', 3: 'Заводские настройки', 5: 'Режимы', 6: 'Считать ID',
            7: 'Сделать фото', 8: 'Выходы', 9: 'Журнал', 10: 'Временные ID', 11: 'SMS',
            12: 'Настройки', 13: 'Абоненты', 14: 'Расписания', 15: 'Группы', 16: 'Праздники',
            17: 'CRC структур', 18: 'CRC ID', 19: 'Обновление', 20: 'Блок прошивки',
            21: 'Получить фото', 22: 'Статус', 23: 'Время'}


class App:
    def __init__(self, server, messages, say, minimized=False):
        self.server, self.messages, self.say = server, messages, say
        self.store = server.data_store
        self.imei = None
        self.root = tk.Tk()
        self.root.title('ДОМОВОЙ · работа с контроллерами')
        self.root.geometry('1260x820')
        self.root.minsize(1060, 720)
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TButton', padding=(10, 6))
        style.configure('Treeview', rowheight=26)
        style.configure('Title.TLabel', font=('Segoe UI', 17, 'bold'))
        style.configure('Hint.TLabel', foreground='#526174')
        self.root.option_add('*Font', ('Segoe UI', 10))
        head = ttk.Frame(self.root, padding=(16, 12))
        head.pack(fill='x')
        ttk.Label(head, text='ДОМОВОЙ / Контроллеры', style='Title.TLabel').pack(side='left')
        ttk.Label(head, text=f'Сервер принимает подключения · порт {server.port}', style='Hint.TLabel').pack(side='right')
        body = ttk.Panedwindow(self.root, orient='horizontal')
        body.pack(fill='both', expand=True, padx=12)
        left = ttk.Frame(body, padding=6)
        right = ttk.Frame(body, padding=6)
        body.add(left, weight=1)
        body.add(right, weight=5)
        ttk.Label(left, text='Устройства', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=6)
        self.peers = self.tree(left, [('name', 'Название', 145), ('online', 'Связь', 75)], height=17)
        self.peers.bind('<<TreeviewSelect>>', self.select)
        ttk.Button(left, text='Добавить / переименовать', command=self.controller_dialog).pack(fill='x', pady=8)
        ttk.Button(left, text='Сохранить логи в ZIP…', command=self.export_logs).pack(fill='x')
        ttk.Label(left, text='Сначала выберите контроллер.\nДанные сохраняются на сервере.\nОтправка — отдельной кнопкой.',
                  style='Hint.TLabel', wraplength=235).pack(anchor='w', pady=16)
        self.heading = ttk.Label(right, text='Выберите контроллер слева', font=('Segoe UI', 12, 'bold'))
        self.heading.pack(anchor='w', pady=(0, 8))
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill='both', expand=True)
        self.pages = {}
        for name in ('Контроллер', 'Доступ', 'Настройки', 'Прошивка', 'Фото и журнал', 'Очередь', 'Обмен'):
            page = ttk.Frame(self.tabs, padding=12)
            self.tabs.add(page, text=name)
            self.pages[name] = page
        self.make_controller()
        self.make_access()
        self.make_settings()
        self.make_firmware()
        self.make_media()
        self.make_queue()
        self.log = tk.Text(self.pages['Обмен'], wrap='word', state='disabled', font=('Consolas', 10))
        self.log.pack(fill='both', expand=True)
        for tag, color in [('bad', '#b42318'), ('good', '#087443'), ('event', '#6941c6')]:
            self.log.tag_config(tag, foreground=color)
        self.notice = tk.StringVar(value='Готово. Действия отправляются только выбранному контроллеру.')
        ttk.Label(self.root, textvariable=self.notice, padding=12, relief='sunken').pack(fill='x')
        self.root.report_callback_exception = lambda cls, exc, tb: self.error(exc)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        if minimized:
            self.root.iconify()
        self.pump()

    def tree(self, parent, columns, height=10):
        outer = ttk.Frame(parent)
        outer.pack(fill='both', expand=True)
        widget = ttk.Treeview(outer, columns=[c[0] for c in columns], show='headings', height=height)
        for key, title, width in columns:
            widget.heading(key, text=title)
            widget.column(key, width=width, minwidth=45)
        scroll = ttk.Scrollbar(outer, orient='vertical', command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        widget.pack(side='left', fill='both', expand=True)
        return widget

    def entry(self, parent, label, value='', width=30):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=30, wraplength=250).pack(side='left')
        var = tk.StringVar(value=value)
        ttk.Entry(row, textvariable=var, width=width).pack(side='left', fill='x', expand=True)
        return var

    def buttons(self, parent, items):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=6)
        for text, callback in items:
            ttk.Button(row, text=text, command=callback).pack(side='left', padx=(0, 6))

    def board(self):
        if not self.imei:
            raise ValueError('Выберите контроллер слева')
        with self.server.peers_lock:
            if self.imei not in self.server.boards:
                from naviset_server import Board
                board = Board(self.say, self.server.profile, self.server.state_dir, self.store)
                board.identify(self.imei)
                board.queue.online = False
                self.server.boards[self.imei] = board
            return self.server.boards[self.imei]

    def command(self, cmd, payload=b'', ttl=None):
        board = self.board()
        uid = board.send(cmd, payload, ttl)
        self.notice.set(f'Команда {cmd} принята в очередь контроллера {self.imei}, №{uid}. Результат — во вкладке «Очередь».')
        return uid

    def error(self, exc):
        self.notice.set(str(exc))
        messagebox.showerror('Не удалось выполнить действие', str(exc), parent=self.root)

    def controller_dialog(self):
        data = self.store.get(self.imei) if self.imei else {}
        window = tk.Toplevel(self.root)
        window.title('Контроллер')
        window.transient(self.root)
        window.grab_set()
        box = ttk.Frame(window, padding=18)
        box.pack(fill='both', expand=True)
        imei = self.entry(box, 'IMEI (15 цифр)', self.imei or '')
        name = self.entry(box, 'Название', data.get('name', ''))
        obj = self.entry(box, 'Объект', data.get('object', ''))

        def save():
            key = imei.get().strip()
            updated = self.store.get(key)
            updated.update(name=name.get().strip() or key, object=obj.get().strip())
            self.store.put(key, updated)
            self.imei = key
            window.destroy()
            self.refresh_peers()
            self.peers.selection_set(key)
            self.load_data()

        ttk.Button(box, text='Сохранить', command=save).pack(anchor='e', pady=10)

    def select(self, event=None):
        selected = self.peers.selection()
        if selected and selected[0] != self.imei:
            self.imei = selected[0]
            self.server.board = self.board()
            self.load_data()

    def make_controller(self):
        page = self.pages['Контроллер']
        self.overview = tk.StringVar(value='Нет выбранного контроллера')
        ttk.Label(page, textvariable=self.overview, justify='left').pack(anchor='w', pady=(0, 10))
        self.buttons(page, [('Обновить статус', lambda: self.command(22)),
                            ('Запросить CRC', lambda: self.command(17)),
                            ('Установить время', lambda: self.command(23, struct.pack('<I', int(time.time())))),
                            ('Перезагрузить', lambda: self.command(2, b'\0'))])
        outputs = ttk.LabelFrame(page, text='Выходы', padding=8)
        outputs.pack(fill='x', pady=5)
        self.buttons(outputs, [('Активировать выход 1', lambda: self.command(8, bytes([3, 1, 0]))),
                               ('Активировать выход 2', lambda: self.command(8, bytes([3, 0, 1])))])
        triggerbox = ttk.LabelFrame(page, text='Режимы контроллера', padding=8)
        triggerbox.pack(fill='x', pady=5)
        self.triggers = []
        for i, text in enumerate(('Ручная разблокировка', 'Разблокировка по сценарию', 'Авторегистрация',
                                  'Открывать всем', 'Пересылать SMS администратору', 'SMS о неизвестном ID', 'SMS о низком балансе')):
            value = tk.BooleanVar()
            self.triggers.append(value)
            ttk.Checkbutton(triggerbox, text=text, variable=value).grid(row=i // 2, column=i % 2, sticky='w', padx=5)
        self.call_schedule = tk.StringVar(value='0')
        ttk.Label(triggerbox, text='Звонки по расписанию (0 — выключено)').grid(row=4, column=0, sticky='w')
        ttk.Spinbox(triggerbox, from_=0, to=64, textvariable=self.call_schedule, width=6).grid(row=4, column=1, sticky='w')
        ttk.Button(triggerbox, text='Применить режимы', command=self.send_triggers).grid(row=5, column=0, pady=8, sticky='w')
        self.buttons(page, [('Прочитать ID Wiegand (15 с)', lambda: self.command(6, bytes([15, 1, 0]))),
                            ('Забрать временные ID', lambda: self.command(10)), ('Сбросить на заводские…', self.factory_reset)])
        self.phone = self.entry(page, 'Телефон SMS (10 цифр)')
        self.sms = self.entry(page, 'Текст SMS (до 50 байт)')
        ttk.Button(page, text='Отправить SMS', command=self.send_sms).pack(anchor='w', pady=4)

    def send_triggers(self):
        values = bytes([int(v.get()) for v in self.triggers] + [integer(self.call_schedule.get(), 0, 64, 'Расписание звонков')])
        self.command(5, values + bytes(8))
        data = self.store.get(self.imei)
        data['triggers'] = list(values)
        self.store.put(self.imei, data)

    def send_sms(self):
        phone = self.phone.get().strip()
        body = self.sms.get().encode('cp1251')
        if len(phone) != 10 or not phone.isascii() or not phone.isdigit() or not 1 <= len(body) <= 50:
            raise ValueError('Нужно 10 цифр телефона и текст от 1 до 50 байт Windows-1251')
        self.command(11, phone.encode('ascii') + body.ljust(50, b'\0'))

    def factory_reset(self):
        self.board()
        if messagebox.askyesno('Заводские настройки', f'Сбросить контроллер {self.imei}?\nНастройки и данные будут потеряны.', parent=self.root):
            self.command(3, b'\0')

    def make_access(self):
        tabs = ttk.Notebook(self.pages['Доступ'])
        tabs.pack(fill='both', expand=True)
        self.tables = {}
        for kind, title in TITLES.items():
            page = ttk.Frame(tabs, padding=8)
            tabs.add(page, text=title)
            labels = {'ids': [('cell', 'Ячейка', 65), ('identifier', 'ID', 130), ('group', 'Группа', 65),
                              ('kind', 'Тип', 85), ('remaining', 'Остаток', 75), ('deleted', 'Удаление', 75)],
                      'groups': [('cell', 'Группа', 70), ('schedule', 'Расписание', 90), ('blocked', 'Заблокирована', 120)],
                      'schedules': [('cell', '№', 55), ('start', 'Начало', 75), ('end', 'Конец', 75),
                                    ('days', 'Пн–Вс', 95), ('blocked', 'Заблокировано', 110)],
                      'holidays': [('date', 'Праздничная дата', 180)]}[kind]
            self.tables[kind] = self.tree(page, labels, height=10)
            self.tables[kind].bind('<Double-1>', lambda event, k=kind: self.edit_row(k, True))
            self.buttons(page, [('Добавить', lambda k=kind: self.edit_row(k)),
                                ('Изменить', lambda k=kind: self.edit_row(k, True)),
                                ('Пометить / снять удаление' if kind == 'ids' else 'Удалить', lambda k=kind: self.delete_row(k))])
            if kind == 'schedules':
                self.scenes = self.entry(page, '8 сцен (номера через запятую)', '0,0,0,0,0,0,0,0')
                self.holiday_mode = self.entry(page, 'Праздники: 1 все, 2 только, 3 кроме', '1')
            ttk.Button(page, text='Сохранить и синхронизировать', command=lambda k=kind: self.synchronize(k)).pack(anchor='w', pady=10)
            text = ('При удалении ID ячейка сохраняется, передаётся истёкшая дата запрета.' if kind == 'ids' else
                    'Отправляется полный снимок выбранной структуры. Неуказанные записи очищаются или блокируются.')
            ttk.Label(page, text=text, style='Hint.TLabel', wraplength=650).pack(anchor='w')

    def edit_row(self, kind, editing=False):
        self.board()
        data = self.store.get(self.imei)
        rows = data.setdefault(kind, [])
        selection = self.tables[kind].selection()
        index = int(selection[0]) if editing and selection else None
        if editing and index is None:
            raise ValueError('Выберите запись')
        row = rows[index] if index is not None else {}
        window = tk.Toplevel(self.root)
        window.title(TITLES[kind])
        window.transient(self.root)
        window.grab_set()
        form = ttk.Frame(window, padding=18)
        form.pack(fill='both', expand=True)
        fields = {}
        for key, title, default in SCHEMAS[kind]:
            default = str(row.get(key, default))
            if key == 'cell' and index is None:
                default = str(max((int(r['cell']) for r in rows), default=0) + 1)
            if key == 'days':
                group = ttk.LabelFrame(form, text=title, padding=8)
                group.pack(fill='x', pady=5)
                fields[key] = [tk.BooleanVar(value=v == '1') for v in default]
                for day, var in zip(('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'), fields[key]):
                    ttk.Checkbutton(group, text=day, variable=var).pack(side='left')
            elif key in ('blocked', 'phone', 'rf433', 'rfid', 'wiegand', 'car', 'link', 'pin'):
                fields[key] = tk.BooleanVar(value=default == '1')
                ttk.Checkbutton(form, text=title, variable=fields[key]).pack(anchor='w', pady=3)
            elif key == 'kind':
                fields[key] = tk.StringVar(value=KINDS[int(default) - 1])
                ttk.Label(form, text=title).pack(anchor='w')
                ttk.Combobox(form, values=KINDS, textvariable=fields[key], state='readonly', width=40).pack(fill='x')
            else:
                fields[key] = self.entry(form, title, default, 34)

        def save():
            result = dict(row)
            for key, control in fields.items():
                result[key] = ''.join('1' if v.get() else '0' for v in control) if isinstance(control, list) else control.get()
                if key == 'kind':
                    result[key] = int(result[key][0])
                elif isinstance(control, tk.BooleanVar):
                    result[key] = int(result[key])
            if kind == 'holidays':
                payloads(kind, {kind: [result]})
            else:
                {'ids': id_record, 'groups': group_record, 'schedules': schedule_record}[kind](result)
                result['cell'] = int(result['cell'])
            unique = 'date' if kind == 'holidays' else 'cell'
            if any(str(r[unique]) == str(result[unique]) for i, r in enumerate(rows) if i != index):
                raise ValueError('Такая ячейка / дата уже есть')
            if index is None:
                rows.append(result)
            else:
                rows[index] = result
            self.store.put(self.imei, data)
            window.destroy()
            self.load_data()
            self.notice.set('Сохранено на сервере. Для передачи нажмите «Сохранить и синхронизировать».')

        ttk.Button(form, text='Сохранить запись', command=save).pack(anchor='e', pady=10)

    def delete_row(self, kind):
        selection = self.tables[kind].selection()
        if not selection:
            raise ValueError('Выберите запись')
        data = self.store.get(self.imei)
        index = int(selection[0])
        if kind == 'ids':
            data[kind][index]['deleted'] = not data[kind][index].get('deleted', False)
        else:
            del data[kind][index]
        self.store.put(self.imei, data)
        self.load_data()

    def synchronize(self, kind):
        board = self.board()
        if kind == 'schedules':
            data = self.store.get(self.imei)
            data.update(scenes=self.scenes.get(), holiday_mode=self.holiday_mode.get())
            payloads(kind, data)
            self.store.put(self.imei, data)
        cmd, frames = self.store.prepare(self.imei, kind)
        with board.lock:
            for row in board.queue.rows(('queued',)):
                if row['cmd'] == cmd:
                    board.queue.finish(row, 'superseded', 'новый снимок структуры')
            board.sync_plan = [(c, p) for c, p in board.sync_plan if c != cmd]
            if board.conn and board.reg:
                board.sync_plan.extend((cmd, frame) for frame in frames)
                board.tick()
        self.notice.set(f'Снимок сохранён: {len(frames)} пакетов. ' +
                        ('Передача начата, результат — «Очередь».' if board.conn else 'Будет отправлен при регистрации контроллера.'))

    def make_settings(self):
        page = self.pages['Настройки']
        ttk.Label(page, text='Настройки контроллера', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(page, text='Основа — сохранённая копия настроек платы (512 байт). Остальные поля сохраняются.',
                  style='Hint.TLabel', wraplength=670).pack(anchor='w', pady=8)
        self.setting_note = tk.StringVar(value='Копия настроек не загружена')
        ttk.Label(page, textvariable=self.setting_note).pack(anchor='w')
        ttk.Button(page, text='Загрузить копию настроек…', command=self.import_settings).pack(anchor='w', pady=8)
        self.settings_fields = {}
        self.setting_choices = {
            'network': ['Автоматически', 'Только 2G', '3G / 4G', 'Только 3G', 'Только 4G'],
            'mode485': ['Пассивный', 'Активный опрос'],
            'baud485': ['115200', '57600', '38400', '19200', '9600'],
            'wiegand': ['Выключен', 'Кодонаборная панель', 'RFID-считыватель', 'Приёмник брелков'],
        }
        self.setting_layout = [('server1', 'Сервер 1 (адрес:порт)', 8, 32), ('server2', 'Сервер 2 (адрес:порт)', 40, 32),
                               ('ntp', 'NTP-сервер', 72, 32), ('apn', 'APN оператора', 104, 32),
                               ('network', 'Мобильная сеть', 136, 1),
                               ('mode485', 'Режим RS-485', 172, 1), ('baud485', 'Скорость RS-485', 174, 1),
                               ('wiegand', 'Устройство Wiegand', 178, 1)]
        for key, label, offset, size in self.setting_layout:
            if key in self.setting_choices:
                row = ttk.Frame(page)
                row.pack(fill='x', pady=3)
                ttk.Label(row, text=label, width=30).pack(side='left')
                value = tk.StringVar(value=self.setting_choices[key][0])
                ttk.Combobox(row, textvariable=value, values=self.setting_choices[key], state='readonly').pack(side='left', fill='x', expand=True)
                self.settings_fields[key] = value
            else:
                self.settings_fields[key] = self.entry(page, label)
        ttk.Label(page, text='Камеры и считыватели RS-485 (адрес / тип)', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=8)
        row = ttk.Frame(page)
        row.pack(fill='x')
        self.peripherals = []
        for i in range(8):
            box = ttk.Frame(row)
            box.pack(side='left', padx=4)
            ttk.Label(box, text=f'№{i+1}').pack()
            a, t = tk.StringVar(value='0'), tk.StringVar(value='0')
            ttk.Entry(box, textvariable=a, width=5).pack()
            ttk.Entry(box, textvariable=t, width=5).pack()
            self.peripherals.append((a, t))
        ttk.Label(page, text='Тип: 0 нет · 1 Wi-Fi · 2 Interceptor · 3 JPEG (OV)', style='Hint.TLabel').pack(anchor='w', pady=8)
        ttk.Button(page, text='Сохранить и отправить настройки', command=self.save_settings).pack(anchor='w', pady=12)

    def import_settings(self):
        self.board()
        path = filedialog.askopenfilename(parent=self.root, filetypes=[('Настройки платы', '*.bin')])
        if not path:
            return
        data = self.store.get(self.imei)
        data['settings'] = Path(path).read_bytes().hex()
        payloads('settings', data)
        self.store.put(self.imei, data)
        self.load_data()

    def save_settings(self):
        self.board()
        data = self.store.get(self.imei)
        original = bytes.fromhex(data.get('settings', ''))
        if len(original) != 512:
            raise ValueError('Загрузите копию настроек')
        result = bytearray(original)
        for key, label, offset, size in self.setting_layout:
            value = self.settings_fields[key].get().strip()
            if size == 1:
                if value not in self.setting_choices[key]:
                    raise ValueError(f'{label}: выберите значение из списка')
                result[offset] = self.setting_choices[key].index(value)
            else:
                raw = value.encode('ascii')
                if len(raw) >= size or b'\0' in raw:
                    raise ValueError(f'{label}: до {size-1} ASCII-символов')
                if key in ('server1', 'server2') and raw:
                    host, sep, port = value.rpartition(':')
                    if not sep or not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
                        raise ValueError(f'{label}: нужен адрес:порт')
                result[offset:offset+size] = raw.ljust(size, b'\0')
        for i, (addr, typ) in enumerate(self.peripherals):
            result[179+i*2] = integer(addr.get(), 0, 255, 'Адрес периферии')
            result[180+i*2] = integer(typ.get(), 0, 255, 'Тип периферии')
        result[510:] = struct.pack('<H', stand.crc16(result[:510]))
        data['settings'] = result.hex()
        self.store.put(self.imei, data)
        self.synchronize('settings')

    def make_firmware(self):
        page = self.pages['Прошивка']
        ttk.Label(page, text='Обновление выбранного контроллера', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        self.firmware = tk.StringVar()
        ttk.Entry(page, textvariable=self.firmware).pack(fill='x', pady=12)
        ttk.Button(page, text='Выбрать файл прошивки…', command=self.pick_firmware).pack(anchor='w')
        self.firmware_info = tk.StringVar(value='Имя файла: HW.FW.номер_сборки.bin')
        ttk.Label(page, textvariable=self.firmware_info, wraplength=660).pack(anchor='w', pady=12)
        self.buttons(page, [('Обновить прошивку', self.update_firmware),
                            ('Откатить на предыдущую…', self.rollback)])
        self.firmware_progress = tk.StringVar(value='Обновление не запускалось')
        ttk.Label(page, textvariable=self.firmware_progress, font=('Segoe UI', 11)).pack(anchor='w', pady=15)
        ttk.Label(page, text='Окончание загрузки не означает успешную установку.\nПосле перезапуска проверьте сборку и событие 136 в журнале.',
                  style='Hint.TLabel').pack(anchor='w')

    def pick_firmware(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[('Прошивки', '*.bin')])
        if path:
            image = stand.Image(path)
            self.firmware.set(path)
            self.firmware_info.set(f'Версия {image.fw} · сборка {image.stamp} · {len(image.body)} байт · {image.blocks} блоков')

    def update_firmware(self):
        board = self.board()
        if not board.conn or not board.reg:
            raise ValueError('Для начала обновления контроллер должен быть на связи')
        path = Path(self.firmware.get())
        hw = int(path.name.split('.')[0])
        image = stand.Image(path)
        if hw != board.reg['hw']:
            raise ValueError(f'Прошивка для HW {hw}, контроллер HW {board.reg["hw"]}')
        if board.details and image.stamp == board.details['build']:
            raise ValueError('Такая сборка уже установлена')
        if not image.body or len(image.body) > 96 * 1024:
            raise ValueError('Размер приложения должен быть от 1 до 96 КиБ')
        with board.lock:
            board.image = image
            board.fw_blocks_sent.clear()
            board.blocks_sent = 0
            board.corrupted = False
        self.command(19, image.cmd19())
        data = self.store.get(self.imei)
        data['firmware_file'] = str(path.resolve())
        data['firmware_header'] = image.cmd19().hex()
        self.store.put(self.imei, data)

    def rollback(self):
        self.board()
        if messagebox.askyesno('Откат', 'Запросить предыдущую прошивку из резервной копии контроллера?', parent=self.root):
            self.command(19, struct.pack('<HIIIH', 1, 0, 0, 0, 0))

    def make_media(self):
        page = self.pages['Фото и журнал']
        self.camera = self.entry(page, 'Адрес камеры', '1')
        self.camera_compat = tk.BooleanVar(value=False)
        ttk.Checkbutton(page, text='Формат текущей прошивки: только адрес камеры (по Протокол2 — 7 + адрес)',
                        variable=self.camera_compat).pack(anchor='w', pady=4)
        self.buttons(page, [('Сделать фото', self.take_photo),
                            ('Забрать фото', lambda: self.command(21, bytes(6))),
                            ('Открыть последнее фото', self.open_photo), ('Забрать журнал', lambda: self.command(9))])
        self.photo_note = tk.StringVar(value='Фото пока нет')
        ttk.Label(page, textvariable=self.photo_note, style='Hint.TLabel').pack(anchor='w', pady=8)
        self.events = self.tree(page, [('time', 'Время UTC', 115), ('event', 'Событие', 440)], height=15)

    def take_photo(self):
        addr = integer(self.camera.get(), 1, 255, 'Адрес камеры')
        self.command(7, bytes([addr]) if self.camera_compat.get() else bytes([7, addr]))

    def open_photo(self):
        board = self.board()
        files = sorted(stand.PHOTOS.glob(f'{board.imei}_*.jpg'), key=lambda p: p.stat().st_mtime)
        if not files:
            raise ValueError('Нет полностью полученных фото')
        os.startfile(str(files[-1]))

    def make_queue(self):
        page = self.pages['Очередь']
        ttk.Label(page, text='Результаты команд и синхронизации', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=8)
        self.commands = self.tree(page, [('uid', '№', 40), ('cmd', 'Команда', 150), ('priority', 'Приоритет', 75),
                                        ('state', 'Состояние', 145), ('reason', 'Ответ / причина', 250)], height=14)
        ttk.Label(page, text='«Отправлено» — ответ ещё не получен. «Результат неизвестен» — связь оборвалась после отправки.',
                  style='Hint.TLabel', wraplength=680).pack(anchor='w', pady=10)
        diagnostic = ttk.LabelFrame(page, text='Условия обмена для ручной проверки', padding=8)
        diagnostic.pack(fill='x')
        self.delay = self.entry(diagnostic, 'Задержка команд, секунд', '0')
        self.fw_delay = self.entry(diagnostic, 'Задержка блока прошивки, секунд', '0')
        ttk.Button(diagnostic, text='Применить задержки', command=self.apply_delays).pack(anchor='w', pady=5)

    def apply_delays(self):
        board = self.board()
        values = [float(self.delay.get()), float(self.fw_delay.get())]
        if any(not 0 <= v <= 120 for v in values):
            raise ValueError('Задержки: 0–120 секунд')
        with board.lock:
            board.profile.update(command_delay=values[0], firmware_delay=values[1])
        self.say(f'[{self.imei}] задержки: команды {values[0]} с, блоки FW {values[1]} с')
        self.notice.set('Задержки применены к следующим командам выбранного контроллера.')

    def load_data(self):
        if not self.imei:
            return
        data = self.store.get(self.imei)
        for kind, tree in self.tables.items():
            tree.delete(*tree.get_children())
            for i, row in enumerate(data.get(kind, [])):
                values = []
                for key in tree['columns']:
                    value = row.get(key, '')
                    if key in ('blocked', 'deleted'):
                        value = 'Да' if value in (True, 1, '1') else 'Нет'
                    elif key == 'kind':
                        value = KINDS[int(value)-1].split(' — ')[1]
                    values.append(value)
                tree.insert('', 'end', iid=str(i), values=values)
        self.scenes.set(data.get('scenes', '0,0,0,0,0,0,0,0'))
        self.holiday_mode.set(data.get('holiday_mode', 1))
        board = self.board()
        defaults = list(board.last_triggers[:8]) if board.last_triggers else [0]*8
        if board.status and not board.last_triggers:
            defaults[:4] = [int(bool(board.status[1] & (1 << bit))) for bit in (7, 6, 5, 4)]
        triggers = data.get('triggers', defaults)
        for v, val in zip(self.triggers, triggers):
            v.set(bool(val))
        self.call_schedule.set(triggers[7])
        raw = bytes.fromhex(data.get('settings', ''))
        self.setting_note.set('Копия настроек загружена' if len(raw) == 512 else 'Копия настроек не загружена')
        for key, _, off, size in self.setting_layout:
            value = (raw[off] if size == 1 else raw[off:off+size].split(b'\0')[0].decode('ascii', 'replace')) if len(raw) == 512 else ''
            if key in self.setting_choices:
                options = self.setting_choices[key]
                value = options[value] if isinstance(value, int) and value < len(options) else ''
            self.settings_fields[key].set(value)
        for i, (a, t) in enumerate(self.peripherals):
            a.set(raw[179+i*2] if len(raw) == 512 else 0)
            t.set(raw[180+i*2] if len(raw) == 512 else 0)
        self.firmware.set(data.get('firmware_file', ''))
        self.delay.set(str(board.profile['command_delay']))
        self.fw_delay.set(str(board.profile['firmware_delay']))

    def refresh_peers(self):
        with self.server.peers_lock:
            boards = dict(self.server.boards)
        with self.store.lock:
            saved = list(self.store.data)
        for imei in dict.fromkeys(saved + list(boards)):
            data = self.store.get(imei)
            values = (data.get('name', imei), 'На связи' if imei in boards and boards[imei].conn else 'Нет связи')
            if self.peers.exists(imei):
                self.peers.item(imei, values=values)
            else:
                self.peers.insert('', 'end', iid=imei, values=values)
        if not self.imei and self.peers.get_children():
            self.peers.selection_set(self.peers.get_children()[0])

    def refresh_board(self):
        if not self.imei:
            return
        board = self.board()
        data = self.store.get(self.imei)
        self.heading.config(text=f'{data.get("name", self.imei)} · {self.imei} · {data.get("object", "")}')
        with board.lock:
            status, details, reg = board.status, board.details, board.reg
            if not board.conn:
                board.tick()
            text = f'Связь: {"есть" if board.conn else "нет"} · сеансов: {board.sessions}'
            if status:
                flags = stand.Board.flags_text(status[1]) if status[1] else 'обычный режим'
                text += f'\nСостояние: {flags} · свободный буфер: {status[2]} Б'
            if reg:
                text += f'\nЖелезо: {reg["hw"]} · протокол: {reg["protocol"]}'
            if details:
                text += f'\nFW {details["firmware"]} · сборка {details["build"]} · сигнал {details["signal"]} · баланс {details["balance"]}'
            self.overview.set(text)
            rows = board.queue.rows(('queued', 'sent', 'done', 'error', 'expired', 'unknown', 'cancelled', 'superseded')) if board.queue else []
            for row in sorted(rows, key=lambda r: r['id'])[-150:]:
                key = f'{self.imei}:{row["id"]}'
                values = (row['uid'], f'{row["cmd"]} · {COMMANDS.get(row["cmd"], "Команда")}', row['priority'], STATES[row['state']], row['reason'])
                if self.commands.exists(key):
                    self.commands.item(key, values=values)
                else:
                    self.commands.insert('', 'end', iid=key, values=values)
            keep = {f'{self.imei}:{r["id"]}' for r in sorted(rows, key=lambda r: r['id'])[-150:]}
            for key in self.commands.get_children():
                if key not in keep:
                    self.commands.delete(key)
            self.events.delete(*self.events.get_children())
            for record in board.journal[-150:]:
                self.events.insert('', 'end', values=(stand.when(record.time), str(record)))
            self.photo_note.set(f'Получено фото в этом сеансе: {len(board.completed_photos)} · последнее смещение: {board.photo_cursor[1]} Б')
            if board.image:
                text = f'Отдано блоков: {len(board.fw_blocks_sent)} / {board.image.blocks} · ожидаем сборку {board.image.stamp}'
                if details and details['build'] == board.image.stamp:
                    confirmed = any(r.type == 136 and r.arg_text().find(str(board.image.stamp)) >= 0 for r in board.journal)
                    text = 'Новая сборка на связи. ' + ('Проба подтверждена: событие 136.' if confirmed else 'Подтверждение пробы в журнале ещё не получено.')
                self.firmware_progress.set(text)

    def export_logs(self):
        board = self.board()
        destination = filedialog.asksaveasfilename(parent=self.root, defaultextension='.zip',
                                                  initialfile=f'domovoi_{self.imei}_{dt.datetime.now():%Y%m%d_%H%M%S}.zip')
        if not destination:
            return
        with tempfile.TemporaryDirectory(prefix='domovoi-logs-') as temporary:
            snapshot = Path(temporary) / 'queue.sqlite3'
            target = sqlite3.connect(snapshot)
            try:
                with board.lock:
                    board.queue.db.backup(target)
            finally:
                target.close()
            with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, 'queue.sqlite3')
                archive.writestr('controller.json', json.dumps(self.store.get(self.imei), ensure_ascii=False, indent=2))
                archive.writestr('profile.json', json.dumps(board.profile, ensure_ascii=False, indent=2))
                for path in (self.server.state_dir / 'traffic').glob(f'{self.imei}_*.jsonl'):
                    archive.write(path, 'traffic/' + path.name)
                for path in stand.PHOTOS.glob(f'{self.imei}_*.jpg'):
                    archive.write(path, 'photos/' + path.name)
        self.notice.set(f'Архив сохранён: {destination}')

    def pump(self):
        for _ in range(300):
            try:
                kind, text = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'show':
                self.root.deiconify()
                self.root.lift()
                continue
            self.log.configure(state='normal')
            self.log.insert('end', f'{dt.datetime.now():%H:%M:%S} {text}\n', kind)
            if int(self.log.index('end-1c').split('.')[0]) > 2000:
                self.log.delete('1.0', '200.0')
            self.log.see('end')
            self.log.configure(state='disabled')
        self.refresh_peers()
        self.refresh_board()
        self.root.after(700, self.pump)

    def close(self):
        if messagebox.askyesno('Закрыть сервер', 'Остановить приём подключений контроллеров?', parent=self.root):
            self.server.stop()
            self.root.destroy()

    def run(self):
        self.root.mainloop()
