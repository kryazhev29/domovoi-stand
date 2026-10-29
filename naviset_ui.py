"""Рабочее место для диагностики и ручной работы с контроллерами."""
import configparser
import csv
import datetime as dt
import json
import os
import queue
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import test_server as stand
from naviset_data import id_record, group_record, schedule_record, payloads, integer, plate_latin
from naviset_dashboard import Dashboard, apply_theme, enable_dpi_awareness
from naviset_photo import PhotoPreview
from rs485_config import load_names


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
ANPR_TYPE = 8               # камера номеров Interceptor
CAMERA_TYPES = (ANPR_TYPE, 9)  # и фотокамера JPEG (OV); типы 485 группами по 8, Протокол2 28.10.2026


def periphery_types():
    """Типы линии 485 по справочнику сервера: periphery.ini рядом со скриптом или exe, иначе список Протокол2."""
    folder = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    try:
        return load_names(folder / 'periphery.ini')['types']
    except (OSError, ValueError, KeyError, configparser.Error):
        return {0: 'Отключено', 1: 'WiFi модем UG164', 8: '485 фотокамера домовой Interceptor',
                9: '485 фотокамера JPEG (OV)', 16: '485 кодовая панель Tantos 100', 24: '485 RFID AXOS'}


PERIPHERY = periphery_types()


def periphery_choice(code):
    return f'{code} — {PERIPHERY.get(code, "нет в справочнике")}'


class App:
    def __init__(self, server, messages, say, minimized=False):
        self.server, self.messages, self.say = server, messages, say
        self.store = server.data_store
        self.imei = None
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.ui_scale = max(1.0, self.root.winfo_fpixels('1i') / 96)
        self.root.title('ДОМОВОЙ · Нависет')
        self.root.geometry(f'{min(round(1180*self.ui_scale), self.root.winfo_screenwidth()-80)}x{min(round(800*self.ui_scale), self.root.winfo_screenheight()-100)}')
        self.root.minsize(round(960*self.ui_scale), round(640*self.ui_scale))
        self.style = apply_theme(self.root)
        self._event_signature = None
        self._closing = False
        self._pump_error = None
        head = ttk.Frame(self.root, padding=(16, 12))
        head.pack(fill='x')
        ttk.Label(head, text='ДОМОВОЙ', style='Title.TLabel').pack(side='left')
        ttk.Label(head, text='  /  Нависет', style='Hint.TLabel').pack(side='left', padx=8)
        self.server_note = tk.StringVar(value=f'●  Сервер запущен · порт {server.port}')
        ttk.Label(head, textvariable=self.server_note, style='Hint.TLabel').pack(side='right')
        body = ttk.Frame(self.root)
        body.pack(fill='both', expand=True, padx=12)
        left = ttk.Frame(body, padding=(14, 16, 14, 6), width=round(235*self.ui_scale), style='Side.TFrame')
        right = ttk.Frame(body, padding=(18, 6, 6, 6))
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        right.pack(side='left', fill='both', expand=True)
        ttk.Label(left, text='ПЛАТЫ', style='Side.TLabel').pack(anchor='w', pady=(4, 8))
        peer_box = ttk.Frame(left, height=round(125*self.ui_scale))
        peer_box.pack(fill='x')
        peer_box.pack_propagate(False)
        self.peers = self.tree(peer_box, [('name', 'Название / IMEI', 132), ('online', 'Связь', 65)], height=4)
        self.peers.tag_configure('online', foreground='#16734b')
        self.peers.tag_configure('offline', foreground='#64748b')
        self.peers.bind('<<TreeviewSelect>>', self.select)
        ttk.Button(left, text='Добавить / изменить…', style='Side.TButton', command=self.controller_dialog).pack(fill='x', pady=(8, 20))
        navigation = ttk.Frame(left, style='Side.TFrame')
        navigation.pack(fill='x')
        self.heading = ttk.Label(right, text='Выберите контроллер слева', style='Hint.TLabel', wraplength=round(750*self.ui_scale))
        self.heading.pack(anchor='w', pady=(0, 8))
        self.tabs = ttk.Notebook(right, style='Desk.TNotebook')
        self.tabs.pack(fill='both', expand=True)
        self.pages = {}
        self.page_hosts, self.nav_buttons, self.canvases = {}, {}, {}
        names = ('Проверка платы', 'Контроллер', 'Доступ', 'Фото и журнал', 'Прошивка', 'Настройки', 'Очередь', 'Обмен')
        for name in names:
            host = ttk.Frame(self.tabs)
            self.tabs.add(host, text=name)
            self.page_hosts[name] = host
            if name in ('Проверка платы', 'Контроллер', 'Настройки', 'Прошивка'):
                page = self.scroll_page(host, name)
            else:
                page = ttk.Frame(host, padding=10)
                page.pack(fill='both', expand=True)
            self.pages[name] = page
            button = ttk.Button(navigation, text='Управление' if name == 'Контроллер' else name,
                                style='Nav.TButton', command=lambda n=name: self.navigate(n))
            button.pack(fill='x', pady=1)
            self.nav_buttons[name] = button
        ttk.Button(left, text='Сохранить логи в ZIP…', style='Side.TButton', command=self.export_logs).pack(side='bottom', fill='x', pady=(4, 10))
        ttk.Button(left, text='Открыть папку логов', style='Side.TButton', command=lambda: os.startfile(stand.LOGS)).pack(side='bottom', fill='x')
        self.dashboard = Dashboard(self, self.pages['Проверка платы'])
        self.make_controller()
        self.make_access()
        self.make_settings()
        self.make_firmware()
        self.make_media()
        self.make_queue()
        self.log = tk.Text(self.pages['Обмен'], wrap='word', state='disabled', font=('Consolas', 10))
        log_scroll = ttk.Scrollbar(self.pages['Обмен'], command=self.log.yview)
        log_scroll.pack(side='right', fill='y')
        self.log.configure(yscrollcommand=log_scroll.set, background='#ffffff', foreground='#344054', relief='flat', padx=10, pady=10)
        self.log.pack(side='left', fill='both', expand=True)
        for tag, color in [('bad', '#b42318'), ('good', '#087443'), ('event', '#6941c6')]:
            self.log.tag_config(tag, foreground=color)
        self.notice = tk.StringVar(value='Готово. Подключите плату и нажмите «Проверить плату».')
        ttk.Label(self.root, textvariable=self.notice, padding=(16, 8), style='Hint.TLabel', wraplength=1050).pack(side='bottom', fill='x', before=body)
        self.root.report_callback_exception = lambda cls, exc, tb: self.error(exc)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind_all('<MouseWheel>', self.scroll_wheel, add='+')
        self.navigate('Проверка платы')
        if minimized:
            self.root.iconify()
        self.pump()

    def navigate(self, name):
        self.tabs.select(self.page_hosts[name])
        for key, button in self.nav_buttons.items():
            button.configure(style='Selected.Nav.TButton' if key == name else 'Nav.TButton')

    def scroll_page(self, host, name):
        canvas = tk.Canvas(host, highlightthickness=0, background='#f3f6fb')
        scrollbar = ttk.Scrollbar(host, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        page = ttk.Frame(canvas, padding=(10, 8, 12, 16))
        item = canvas.create_window(0, 0, window=page, anchor='nw')
        page.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(item, width=event.width))
        self.canvases[str(page)] = canvas
        return page

    def scroll_wheel(self, event):
        widget = event.widget
        if isinstance(widget, (ttk.Treeview, tk.Text, ttk.Combobox, ttk.Spinbox)):
            return
        while widget is not None:
            canvas = self.canvases.get(str(widget))
            if canvas:
                if canvas.yview() != (0.0, 1.0):
                    canvas.yview_scroll(-int(event.delta / 120), 'units')
                return 'break'
            widget = getattr(widget, 'master', None)

    def tree(self, parent, columns, height=10):
        outer = ttk.Frame(parent)
        outer.pack(fill='both', expand=True)
        widget = ttk.Treeview(outer, columns=[c[0] for c in columns], show='headings', height=height)
        for key, title, width in columns:
            widget.heading(key, text=title)
            widget.column(key, width=round(width*self.ui_scale), minwidth=round(45*self.ui_scale))
        scroll = ttk.Scrollbar(outer, orient='vertical', command=widget.yview)
        horizontal = ttk.Scrollbar(outer, orient='horizontal', command=widget.xview)
        widget.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        widget.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
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
        if not board.conn:
            raise ValueError('Плата не на связи. Подключите её и повторите действие.')
        if board.check_lock.locked():
            raise ValueError('Дождитесь завершения проверки платы или остановите её.')
        if cmd in (2, 5, 8, 11):
            action = COMMANDS.get(cmd, str(cmd))
            if not messagebox.askyesno(action, f'{action} · IMEI {self.imei}\nВыполнить на выбранной плате?', parent=self.root):
                return None
        uid = board.send(cmd, payload, ttl)
        self.notice.set(f'{COMMANDS.get(cmd, "Команда")} · принято в очередь · IMEI {self.imei} · №{uid}. Результат — «Очередь».')
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
        if self.command(5, values + bytes(8)) is None:
            return
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

    def edit_row(self, kind, editing=False, preset=None):
        self.board()
        data = self.store.get(self.imei)
        rows = data.setdefault(kind, [])
        selection = self.tables[kind].selection()
        index = int(selection[0]) if editing and selection else None
        if editing and index is None:
            raise ValueError('Выберите запись')
        row = rows[index] if index is not None else dict(preset or {})
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
            if kind == 'ids' and result['kind'] == 3:
                result['identifier'] = plate_latin(result['identifier'])  # как отдаёт камера номеров
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
        if board.check_lock.locked():
            raise ValueError('Дождитесь окончания проверки платы')
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
        ttk.Label(page, text='Линия RS-485: что включено', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(14, 4))
        ttk.Label(page, text='Устройства по адресам работают в режиме «Активный опрос». Камера номеров Interceptor — '
                             'адрес 48: в её настройках serialAddr 48, это символ «0». Wi-Fi-модем плата включает по USB '
                             '(NextConfig), а не по этой таблице; пока он выбран, линия 485 занята мостом. '
                             '«Сейчас» — ответы устройств по последнему статусу 2 платы.',
                  style='Hint.TLabel', wraplength=670).pack(anchor='w')
        grid = ttk.Frame(page)
        grid.pack(fill='x', pady=4)
        for column, title in enumerate(('№', 'Устройство', 'Адрес', 'Сейчас')):
            ttk.Label(grid, text=title, style='Hint.TLabel').grid(row=0, column=column, sticky='w', padx=4)
        self.peripherals = []
        choices = [periphery_choice(code) for code in PERIPHERY]
        for i in range(8):
            kind, addr, state = tk.StringVar(value=choices[0]), tk.StringVar(value='0'), tk.StringVar(value='—')
            ttk.Label(grid, text=str(i + 1)).grid(row=i + 1, column=0, sticky='w', padx=4)
            ttk.Combobox(grid, textvariable=kind, values=choices, state='readonly', width=38).grid(row=i + 1, column=1, sticky='w', padx=4, pady=1)
            ttk.Spinbox(grid, textvariable=addr, from_=0, to=255, width=6).grid(row=i + 1, column=2, sticky='w', padx=4)
            ttk.Label(grid, textvariable=state).grid(row=i + 1, column=3, sticky='w', padx=4)
            self.peripherals.append((kind, addr, state))
        self.buttons(page, [('Сохранить и отправить настройки', self.save_settings),
                            ('Обновить «Сейчас» (статус 2)', lambda: self.command(22))])

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
        if self.board().check_lock.locked():
            raise ValueError('Дождитесь окончания проверки платы')
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
        used = set()
        for i, (kind, addr, _) in enumerate(self.peripherals):
            code = integer(kind.get().split(' — ')[0], 0, 255, f'Устройство 485 №{i+1}')
            address = integer(addr.get(), 0, 255, f'Адрес устройства 485 №{i+1}')
            if code and not address:
                raise ValueError(f'Устройство 485 №{i+1}: адрес 1–255, адрес 0 — резерв')
            if code and address in used:
                raise ValueError(f'Адрес {address} на линии 485 уже занят другим устройством')
            used.add(address if code else None)
            result[179+i*2], result[180+i*2] = (address, code) if code else (0, 0)
        if used - {None} and not result[172]:
            raise ValueError('Устройства по адресам работают только в режиме «Активный опрос»: выберите его в «Режим RS-485»')
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
        if board.check_lock.locked() or board.queue.flags & 0x800 or any(r['cmd'] in (19, 20) for r in board.queue.rows()):
            raise ValueError('Проверка или обновление уже выполняется. Дождитесь завершения.')
        path = Path(self.firmware.get())
        hw = int(path.name.split('.')[0])
        image = stand.Image(path)
        if hw != board.reg['hw']:
            raise ValueError(f'Прошивка для HW {hw}, контроллер HW {board.reg["hw"]}')
        if board.details and image.stamp == board.details['build']:
            raise ValueError('Такая сборка уже установлена')
        if not image.body or len(image.body) > 96 * 1024:
            raise ValueError('Размер приложения должен быть от 1 до 96 КиБ')
        if not messagebox.askyesno('Обновление прошивки',
                                  f'IMEI {self.imei}\nУстановить FW {image.fw}, сборку {image.stamp}?\nПлата будет перезапущена.', parent=self.root):
            return
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
        ttk.Label(page, text='Камера и события', style='Title.TLabel').pack(anchor='w', pady=(0, 8))
        camera_row = ttk.Frame(page)
        camera_row.pack(fill='x')
        ttk.Label(camera_row, text='Адрес камеры').pack(side='left')
        self.camera = tk.StringVar(value='1')
        ttk.Spinbox(camera_row, textvariable=self.camera, from_=1, to=255, width=5).pack(side='left', padx=8)
        ttk.Button(camera_row, text='Сделать снимок', style='Accent.TButton', command=self.take_photo).pack(side='left', padx=4)
        ttk.Button(camera_row, text='Получить фото', command=lambda: self.command(21, bytes(6))).pack(side='left', padx=4)
        ttk.Button(camera_row, text='Оригинал ↗', command=self.open_photo).pack(side='right')
        compatibility = ttk.Frame(page)
        compatibility.pack(fill='x', pady=6)
        ttk.Label(compatibility, text='Формат камеры').pack(side='left', padx=(0, 8))
        self.camera_format = tk.StringVar(value='ДОМОВОЙ NEXT')
        ttk.Combobox(compatibility, textvariable=self.camera_format, values=('ДОМОВОЙ NEXT', 'Протокол2'),
                     state='readonly', width=20).pack(side='left')
        plate = ttk.LabelFrame(page, text='Камера номеров Interceptor (тип 8)', padding=8)
        plate.pack(fill='x', pady=(2, 4))
        self.plate_note = tk.StringVar(value='Номер с камеры ещё не считывали')
        ttk.Label(plate, textvariable=self.plate_note, wraplength=680).pack(anchor='w')
        self.buttons(plate, [('Считать номер с камеры (15 с)', self.read_plate),
                             ('Добавить номер в базу…', self.add_plate)])
        ttk.Label(plate, text='Номер считывает плата по адресу камеры выше. Снимок камера отдаёт, только пока номер в кадре: '
                              'держите номер перед камерой и нажмите «Сделать снимок». Для прохода номер нужен в базе, '
                              'а группа — с разрешением «Автономер».', style='Hint.TLabel', wraplength=680).pack(anchor='w')
        self.photo_preview = PhotoPreview(page)
        self.photo_preview.canvas.configure(height=round(270*self.ui_scale))
        self.photo_preview.pack(fill='both', expand=True, pady=(6, 0))
        self.photo_note = tk.StringVar(value='Фото пока нет')
        ttk.Label(page, textvariable=self.photo_note, style='Hint.TLabel').pack(anchor='w', pady=8)
        event_head = ttk.Frame(page)
        event_head.pack(fill='x')
        ttk.Label(event_head, text='Журнал событий', style='Subtitle.TLabel').pack(side='left')
        ttk.Button(event_head, text='Получить журнал', command=lambda: self.command(9)).pack(side='right', pady=3)
        self.events = self.tree(page, [('time', 'Время UTC', 115), ('event', 'Событие', 440)], height=4)

    def take_photo(self):
        addr = integer(self.camera.get(), 1, 255, 'Адрес камеры')
        self.command(7, bytes([addr]) if self.camera_format.get() == 'ДОМОВОЙ NEXT' else bytes([7, addr]))
        data = self.store.get(self.imei)
        data.update(camera_address=addr, camera_format=self.camera_format.get())
        self.store.put(self.imei, data)

    def read_plate(self):
        """Захват 6: источник 485, адрес камеры; номер придёт пакетом 8."""
        self.command(6, bytes([15, 3, integer(self.camera.get(), 1, 255, 'Адрес камеры')]))

    def add_plate(self):
        """Форма ID «Автономер» с последним считанным номером; передача — «Сохранить и синхронизировать»."""
        reader = self.board().last_reader
        plate = stand.text10(reader[2]) if reader and reader[1] == 1 else ''
        self.navigate('Доступ')
        self.edit_row('ids', preset={'identifier': plate, 'kind': 3})

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
        raw = bytes.fromhex(data.get('settings', ''))
        cameras = [raw[179+i*2] for i in range(8) if len(raw) == 512 and raw[180+i*2] in CAMERA_TYPES and raw[179+i*2]]
        saved = data.get('camera_address', 1)
        self.camera.set(str(saved if saved in cameras or not cameras else cameras[0]))  # адрес из таблицы 485 важнее старого
        self.camera_format.set(data.get('camera_format', 'ДОМОВОЙ NEXT'))
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
        self.setting_note.set('Копия настроек загружена' if len(raw) == 512 else 'Копия настроек не загружена')
        for key, _, off, size in self.setting_layout:
            value = (raw[off] if size == 1 else raw[off:off+size].split(b'\0')[0].decode('ascii', 'replace')) if len(raw) == 512 else ''
            if key in self.setting_choices:
                options = self.setting_choices[key]
                value = options[value] if isinstance(value, int) and value < len(options) else ''
            self.settings_fields[key].set(value)
        for i, (kind, addr, _) in enumerate(self.peripherals):
            kind.set(periphery_choice(raw[180+i*2] if len(raw) == 512 else 0))
            addr.set(raw[179+i*2] if len(raw) == 512 else 0)
        self.firmware.set(data.get('firmware_file', ''))
        self.firmware_info.set('Имя файла: HW.FW.номер_сборки.bin')
        self.firmware_progress.set('Обновление не запускалось для этой платы')
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
                self.peers.item(imei, values=values, tags=('online' if imei in boards and boards[imei].conn else 'offline',))
            else:
                self.peers.insert('', 'end', iid=imei, values=values,
                                  tags=('online' if imei in boards and boards[imei].conn else 'offline',))
        if not self.imei and self.peers.get_children():
            online = [imei for imei, board in boards.items() if board.conn]
            self.peers.selection_set(online[0] if online else self.peers.get_children()[0])

    def refresh_board(self):
        if not self.imei:
            self.dashboard.refresh(None)
            self.photo_preview.refresh(None, stand.PHOTOS)
            return
        board = self.board()
        data = self.store.get(self.imei)
        self.photo_preview.refresh(self.imei, stand.PHOTOS)
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
            self.dashboard.refresh(board)
            rows = board.queue.history() if board.queue else []
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
            signature = (self.imei, tuple(str(r) for r in board.journal[-150:]))
            if signature != self._event_signature:
                position = self.events.yview()[0]
                self.events.delete(*self.events.get_children())
                for record in board.journal[-150:]:
                    self.events.insert('', 'end', values=(stand.when(record.time), str(record)))
                self.events.yview_moveto(position if self._event_signature and self._event_signature[0] == self.imei else 0)
                self._event_signature = signature
            self.photo_note.set(f'Получено фотографий: {len(board.completed_photos)} · ' +
                                ('Принимаем фото…' if board.busy_photo else 'Ожидание следующего снимка'))
            # Ответы устройств 485 — состояние периферии статуса 2; включённые — по сохранённой копии настроек.
            states = bytes.fromhex(details['peripherals']) if details and details.get('peripherals') else b''
            settings = bytes.fromhex(data.get('settings', ''))
            settings = settings if len(settings) == 512 else bytes(512)
            for i, (_, _, state) in enumerate(self.peripherals):
                state.set('—' if not settings[180+i*2] or len(states) < 8 else 'отвечает' if states[i] else 'нет ответа')
            text = 'Номер с камеры ещё не считывали'
            if board.last_reader:
                stamp, code, raw = board.last_reader
                text = ((f'Считан номер «{stand.text10(raw)}»' if code == 1 else 'Номер не считан: время вышло') +
                        f' · {dt.datetime.fromtimestamp(stamp):%H:%M:%S}')
            camera = next((i for i in range(8) if settings[180+i*2] == ANPR_TYPE), None)
            if camera is not None and len(states) == 8:
                text += f' · камера номеров, адрес {settings[179+camera*2]}: ' + ('отвечает' if states[camera] else 'нет ответа')
            self.plate_note.set(text)
            if not board.image:
                self.firmware_progress.set('Обновление не запускалось для этой платы')
            else:
                text = f'Отдано блоков: {len(board.fw_blocks_sent)} / {board.image.blocks} · ожидаем сборку {board.image.stamp}'
                if details and details['build'] == board.image.stamp:
                    confirmed = any(r.type == 136 and r.arg_text().find(str(board.image.stamp)) >= 0 for r in board.journal)
                    text = 'Новая сборка на связи. ' + ('Проба подтверждена: событие 136.' if confirmed else 'Подтверждение пробы в журнале ещё не получено.')
                self.firmware_progress.set(text)

    def export_logs(self):
        """Всё для разработчика сервера: кадры обмена, журнал сервера, отчёты, очередь и фото.
        Плата выбрана — только её файлы, не выбрана — все платы."""
        imei = self.imei or '*'
        desktop = Path.home() / 'Desktop'
        destination = filedialog.asksaveasfilename(parent=self.root, defaultextension='.zip', filetypes=[('Архив ZIP', '*.zip')],
                                                  initialdir=desktop if desktop.is_dir() else Path.home(),
                                                  initialfile=f'domovoi_{self.imei or "все"}_{dt.datetime.now():%Y%m%d_%H%M%S}.zip')
        if not destination:
            return
        count = 0
        with tempfile.TemporaryDirectory(prefix='domovoi-logs-') as temporary, \
                zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
            with self.server.peers_lock:
                boards = [b for b in self.server.boards.values() if b.queue and (imei == '*' or b.imei == imei)]
            for board in boards:  # открытые базы очередей — через backup, иначе снимок может быть неполным
                snapshot = Path(temporary) / f'{board.imei}.sqlite3'
                target = sqlite3.connect(snapshot)
                try:
                    with board.lock:
                        board.queue.db.backup(target)
                finally:
                    target.close()
                archive.write(snapshot, f'queue/{board.imei}.sqlite3')
                archive.writestr(f'queue/{board.imei}_profile.json', json.dumps(board.profile, ensure_ascii=False, indent=2))
                archive.writestr(f'queue/{board.imei}_controller.json', json.dumps(self.store.get(board.imei), ensure_ascii=False, indent=2))
                count += 1
            for folder, pattern, name in ((self.server.state_dir / 'traffic', f'{imei}_*.jsonl', 'traffic'),
                                          (self.server.state_dir / 'reports', f'проверка_{imei}_*.*', 'reports'),
                                          (stand.PHOTOS, f'{imei}_*.jpg', 'photos'),
                                          (stand.LOGS, 'адаптер_*.log', 'server')):
                for path in sorted(folder.glob(pattern)) if folder.is_dir() else []:
                    archive.write(path, f'{name}/{path.name}')
                    count += 1
            archive.writestr('ЧТО_ВНУТРИ.txt', 'traffic — каждый кадр обмена с платой в обе стороны (JSON Lines: время UTC, направление, hex, '
                             'команда, расшифровка).\nserver — журнал сервера по дням (то же, что вкладка «Обмен»).\n'
                             'reports — отчёты «Проверка платы».\nqueue — очередь команд и журнал событий платы (SQLite), профиль, карточка.\n'
                             'photos — снимки с камеры.\n')
        self.notice.set(f'Архив сохранён, файлов: {count} — {destination}')

    def pump(self):
        try:
            self.pump_once()
            self._pump_error = None
        except Exception as exc:
            self.notice.set(f'Не удалось обновить экран: {exc}. Повторяем автоматически.')
            if str(exc) != self._pump_error:
                self.say(f'ошибка обновления окна: {exc}', 'bad')
                self._pump_error = str(exc)
        finally:
            if not self._closing:
                self.root.after(700, self.pump)

    def pump_once(self):
        for _ in range(300):
            try:
                kind, text = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'show':
                self.root.deiconify()
                self.root.lift()
                continue
            if kind == 'diagnostic':
                self.dashboard.receive(text)
                continue
            follow = self.log.yview()[1] >= .99
            self.log.configure(state='normal')
            self.log.insert('end', f'{dt.datetime.now():%H:%M:%S} {text}\n', kind)
            if int(self.log.index('end-1c').split('.')[0]) > 2000:
                self.log.delete('1.0', '200.0')
            if follow:
                self.log.see('end')
            self.log.configure(state='disabled')
        self.refresh_peers()
        self.refresh_board()
        self.server_note.set(f'●  Сервер запущен · порт {self.server.port}' if not self.server.error and not self.server.stopping
                             else 'Сервер остановлен · проверьте обмен')

    def close(self):
        if messagebox.askyesno('Закрыть сервер', 'Остановить приём подключений контроллеров?', parent=self.root):
            self._closing = True
            for runner in self.dashboard.runners.values():
                runner.cancel.set()
            self.server.stop()
            self.root.destroy()

    def run(self):
        self.root.mainloop()
