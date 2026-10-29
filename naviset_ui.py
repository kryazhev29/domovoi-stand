"""Рабочее место для диагностики и ручной работы с контроллерами."""
import configparser
import csv
import datetime as dt
import json
import os
import queue
import re
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
from naviset_photo import ShotPanel, fit_text
from naviset_shots import (CAMERAS, CAUSES, FROM_TABLE, JPEG_TYPE, PLATE_TYPE, SHOT_SLACK, SHOT_WAIT, answer_text,
                           camera_address, camera_of_address, camera_state, latest_plate, list_shots, local_when,
                           reader_text, shot_done, shot_failed, table_addresses)
from naviset_queue import answer_code
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
            7: 'Сделать снимок', 8: 'Выходы', 9: 'Журнал', 10: 'Временные ID', 11: 'SMS',
            12: 'Настройки', 13: 'Абоненты', 14: 'Расписания', 15: 'Группы', 16: 'Праздники',
            17: 'CRC структур', 18: 'CRC ID', 19: 'Обновление', 20: 'Блок прошивки',
            21: 'Получить фото', 22: 'Статус', 23: 'Время'}
CAMERA_TABS = {'JPEG-камера': 'jpeg', 'Камера номеров': 'plate'}   # 8 — камера номеров, 9 — фотокамера JPEG (OV)
TAB_TYPES = {'JPEG-камера': JPEG_TYPE, 'Камера номеров': PLATE_TYPE}
PASS_EVENTS = {69, 70, 71, 72, 73, 74, 75, 76, 77, 79, 80, 131}  # у них «проходы» в журнале значимы


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
        ttk.Button(left, text='Добавить / изменить…', style='Side.TButton', command=self.controller_dialog).pack(fill='x', pady=(8, 12))
        navigation = ttk.Frame(left, style='Side.TFrame')
        navigation.pack(fill='x')
        self.heading = ttk.Label(right, text='Выберите контроллер слева', style='Hint.TLabel', wraplength=round(750*self.ui_scale))
        self.heading.pack(anchor='w', pady=(0, 8))
        self.tabs = ttk.Notebook(right, style='Desk.TNotebook')
        self.tabs.pack(fill='both', expand=True)
        self.pages = {}
        self.page_hosts, self.nav_buttons, self.canvases = {}, {}, {}
        names = ('Проверка платы', 'Контроллер', 'Доступ', 'JPEG-камера', 'Камера номеров', 'Журнал',
                 'Прошивка', 'Настройки', 'Очередь', 'Обмен')
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
        # Команды вкладок камер для «Последней команды», список снимков и адреса камер.
        self.pending, self.shots, self._shots_key = {}, [], None
        self.address_notes, self.address_hints, self.camera_states, self.auto_address = {}, {}, {}, {}
        self.make_jpeg()
        self.make_plate()
        self.make_journal()
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
        self.apply_camera_addresses(force=True)

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
        self.apply_camera_addresses(force=True)  # новые строки 485 сразу видны во вкладках камер
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

    def camera_head(self, page, title, hint):
        ttk.Label(page, text=title, style='Title.TLabel').pack(anchor='w')
        fit_text(ttk.Label(page, text=hint, style='Hint.TLabel')).pack(fill='x', pady=(2, 12))

    def card(self, parent, title, wrap):
        """Белая карточка как на главной: подпись заглавными и значение."""
        box = ttk.Frame(parent, style='Card.TFrame', padding=14)
        ttk.Label(box, text=title, style='Card.TLabel').pack(anchor='w')
        value = ttk.Label(box, text='—', style='Card.TLabel', font=('Segoe UI', 11), justify='left', wraplength=round(wrap*self.ui_scale))
        value.pack(anchor='w', pady=(6, 0))
        return box, value

    def camera_controls(self, page, tab, address):
        """Одинаково на обеих вкладках: адрес, пояснение под полем, «Камера: …» по статусу 2 и кнопка обновления."""
        row = ttk.Frame(page)
        row.pack(fill='x')
        ttk.Label(row, text='Адрес камеры').pack(side='left')
        ttk.Spinbox(row, textvariable=address, from_=1, to=255, width=5).pack(side='left', padx=8)
        self.address_notes[tab] = fit_text(ttk.Label(page, text='', style='Hint.TLabel', justify='left'))
        self.address_notes[tab].pack(fill='x', pady=(4, 0))
        line = ttk.Frame(page)
        line.pack(fill='x', pady=(8, 0))
        ttk.Button(line, text='Обновить (статус 2)', command=lambda: self.command(22)).pack(side='right', anchor='n', padx=(12, 0))
        self.camera_states[tab] = fit_text(ttk.Label(line, text='Камера: неизвестно', justify='left'))
        self.camera_states[tab].pack(side='left', fill='x', expand=True)
        return row

    def make_jpeg(self):
        page = self.pages['JPEG-камера']
        self.camera_head(page, 'JPEG-камера', 'Фотокамера на линии 485 (тип 9). Снимает при проходе, по тревоге и по кнопке «Сделать снимок».')
        self.jpeg_address = tk.StringVar()
        row = self.camera_controls(page, 'JPEG-камера', self.jpeg_address)
        ttk.Button(row, text='Сделать снимок', style='Accent.TButton', command=self.take_jpeg).pack(side='left', padx=(4, 0))
        self.jpeg_note, self.jpeg_state = self.address_notes['JPEG-камера'], self.camera_states['JPEG-камера']
        box, self.jpeg_result = self.card(page, 'ПОСЛЕДНЯЯ КОМАНДА', 820)
        box.pack(fill='x', pady=(12, 12))
        self.jpeg_shots = ShotPanel(page, self, 'jpeg', [('time', 'Время', 100), ('cause', 'Причина', 330),
                                                         ('size', 'Размер', 65)], lambda: stand.PHOTOS)
        self.jpeg_shots.pack(fill='both', expand=True)

    def make_plate(self):
        page = self.pages['Камера номеров']
        self.camera_head(page, 'Камера номеров', 'Interceptor на линии 485 (тип 8). Читает номер машины; '
                                                 'снимок отдаёт, только пока номер в кадре.')
        self.plate_address = tk.StringVar(value='48')
        self.camera_controls(page, 'Камера номеров', self.plate_address)
        self.plate_note, self.plate_state = self.address_notes['Камера номеров'], self.camera_states['Камера номеров']
        actions = ttk.Frame(page)
        actions.pack(fill='x', pady=(10, 12))
        for text, callback, style in (('Считать номер и снять', lambda: self.read_plate(True), 'Accent.TButton'),
                                      ('Считать номер (15 с)', self.read_plate, 'TButton'),
                                      ('Сделать снимок', self.shoot_plate, 'TButton'),
                                      ('Добавить номер в базу…', self.add_plate, 'TButton')):
            ttk.Button(actions, text=text, style=style, command=callback).pack(side='left', padx=(0, 6))
        cards = ttk.Frame(page)
        cards.pack(fill='x', pady=(0, 12))
        cards.columnconfigure(0, weight=1, uniform='card')
        cards.columnconfigure(1, weight=2, uniform='card')
        box, self.plate_number = self.card(cards, 'ПОСЛЕДНИЙ НОМЕР', 260)
        box.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.plate_number.configure(font=('Segoe UI', 17, 'bold'))
        self.plate_when = ttk.Label(box, text='', style='Card.TLabel', foreground='#64748b', justify='left',
                                    wraplength=round(260*self.ui_scale))
        self.plate_when.pack(anchor='w')
        box, self.plate_result = self.card(cards, 'ПОСЛЕДНЯЯ КОМАНДА', 520)
        box.grid(row=0, column=1, sticky='nsew')
        self.plate_shots = ShotPanel(page, self, 'plate', [('time', 'Время', 100), ('plate', 'Номер машины', 110),
                                                           ('reason', 'Причина', 230), ('size', 'Размер', 65)], lambda: stand.PHOTOS)
        self.plate_shots.pack(fill='both', expand=True)

    def make_journal(self):
        page = self.pages['Журнал']
        head = ttk.Frame(page)
        head.pack(fill='x')
        ttk.Label(head, text='Журнал событий', style='Title.TLabel').pack(side='left')
        ttk.Button(head, text='Получить журнал', command=lambda: self.command(9)).pack(side='right')
        fit_text(ttk.Label(page, text='Последние 150 записей платы, новые сверху. Время местное, по часам этого компьютера '
                                      '(плата хранит его в UTC). Новые записи «Нависет» забирает сам, когда плата сообщает '
                                      'о них; кнопка — запросить сейчас.',
                           style='Hint.TLabel')).pack(fill='x', pady=(2, 12))
        self.events = self.tree(page, [('time', 'Время', 125), ('code', 'Код', 55), ('event', 'Событие', 280),
                                       ('detail', 'Данные', 300)], height=16)
        for key in ('time', 'code'):
            self.events.column(key, stretch=False)

    def remember(self, key, value):
        data = self.store.get(self.imei)
        data[key] = value
        self.store.put(self.imei, data)

    def camera_field(self, variable, camera):
        """Адрес из поля; пустое поле — подсказка по-русски, а не ошибка Python."""
        if not variable.get().strip():
            raise ValueError(f'Введите адрес {camera} (1–255) или загрузите копию настроек во вкладке «Настройки»')
        return integer(variable.get(), 1, 255, f'Адрес {camera}')

    def take_jpeg(self):
        """Команда 7: один байт адреса, как принимает прошивка."""
        address = self.camera_field(self.jpeg_address, 'JPEG-камеры')
        self.camera_command('JPEG-камера', 'Сделать снимок', 7, address)
        self.remember('jpeg_address', address)

    def plate_camera(self):
        address = self.camera_field(self.plate_address, 'камеры номеров')
        self.remember('plate_address', address)
        return address

    def read_plate(self, shoot=False):
        """Захват 6: 15 с, источник 485, адрес камеры; номер придёт пакетом 8. shoot — сразу за номером команда 7."""
        address = self.plate_camera()
        self.camera_command('Камера номеров', 'Считать номер и снять' if shoot else 'Считать номер', 6, address,
                            bytes([15, 3, address]), shoot)

    def shoot_plate(self):
        self.camera_command('Камера номеров', 'Сделать снимок', 7, self.plate_camera())

    def camera_command(self, tab, label, cmd, address, payload=None, shoot=False, prefix=''):
        """Отправить и запомнить, чтобы карточка «Последняя команда» вкладки показала ответ платы и итог.
        Отметки — до отправки: ответ модели платы приходит раньше, чем send() вернётся.
        Камера для текстов — по типу строки 485 с этим адресом, если копия настроек это знает."""
        board = self.board()
        raw = bytes.fromhex(self.store.get(self.imei).get('settings', ''))
        op = dict(imei=self.imei, cmd=cmd, address=address, shoot=shoot, prefix=prefix, answer=None, answered=None,
                  started=time.time(), mark=time.perf_counter(), number=None, final=None,
                  camera=camera_of_address(raw, address) or CAMERA_TABS[tab],
                  files={p.name for p in stand.PHOTOS.glob(f'{self.imei}_*.jpg')},
                  seen=board.journal[-1].index if board.journal else -1)
        uid = self.command(cmd, bytes([address]) if payload is None else payload)
        if uid is None:
            return
        self.notice.set(f'{label} · принято в очередь (№{uid}) — результат в карточке «Последняя команда»')
        self.pending[tab] = dict(op, uid=uid)

    @staticmethod
    def finish(op, text, tone):
        """Итог команды запоминается: дальше его не пересчитываем."""
        op['final'] = text, tone
        return op['final']

    def camera_result(self, tab, board):
        """(текст, тон) по ответу 11, пакету 8, событию 129 и новому файлу снимка."""
        op = self.pending.get(tab)
        if not op or op['imei'] != self.imei:
            return 'С этой вкладки команд ещё не было', ''
        if op['final']:
            return op['final']
        if op['answer'] is None:
            row = board.queue.latest(op['uid'], op['cmd']) if board.queue else None
            code = next((answer_code(p, op['cmd']) for t, u, c, p in reversed(board.inbox)
                         if t >= op['mark'] and u == op['uid'] and c == 11), None)
            if code is None and row and row['state'] in ('done', 'error'):
                found = re.search(r'код (\d+)', row['reason'] or '')
                code = 0 if row['state'] == 'done' else int(found.group(1)) if found else None
            if code is None:
                state = row['state'] if row else 'unknown'
                if state in ('queued', 'sent'):
                    return op['prefix'] + ('Команда в очереди, ждём плату…' if state == 'queued' else 'Команда отправлена, ждём ответ платы…'), ''
                return self.finish(op, op['prefix'] + f'Ответа платы нет: {STATES[state].lower()}' +
                                   (f' — {row["reason"]}' if row and row['reason'] else ''), 'bad')
            op['answer'], op['answered'] = code, time.time()
        if op['answer']:
            return self.finish(op, op['prefix'] + answer_text(op['cmd'], op['answer'], op['camera'], op['address']), 'bad')
        waited = time.time() - op['answered']
        if op['cmd'] == 6:
            reader = board.last_reader
            if reader and reader[0] >= op['started']:
                text = reader_text(reader[1], reader[2], op['address'])
                if reader[1] == 1 and op['shoot']:
                    op['shoot'] = False   # «Считать номер и снять»: камера отдаёт кадр, пока видела номер последние 5 с
                    try:
                        self.camera_command(tab, 'Сделать снимок', 7, op['address'], prefix=text + ' · ')
                    except ValueError as exc:
                        return self.finish(op, f'{text} · снимок не отправлен: {exc}', 'bad')
                    return text + ' · снимаем…', ''
                return self.finish(op, text, 'good' if reader[1] == 1 else 'bad')
            if waited > 20:
                return self.finish(op, 'Номер не пришёл за 20 с — проверьте связь с платой (подробности в «Обмене»)', 'bad')
            return answer_text(6, 0, op['camera'], op['address']), ''
        # Свой снимок: номер из события 129 после команды или время съёмки рядом со временем команды.
        for record in board.journal[-40:]:
            if record.type == 129 and record.index > op['seen'] and not op['number']:
                op['number'] = struct.unpack_from('<I', record.arg1)[0]
        fresh = sorted((s for s in self.shots if s['name'] not in op['files'] and (s['number'] == op['number'] or
                        op['started'] - SHOT_SLACK <= s['number'] <= op['started'] + SHOT_WAIT + SHOT_SLACK)),
                       key=lambda s: s['number'] != op['number'])
        if fresh:
            return self.finish(op, op['prefix'] + shot_done(fresh[0], op['camera']), 'good')
        if waited > SHOT_WAIT:
            if board.busy_photo and waited < 3 * SHOT_WAIT:
                return op['prefix'] + 'Снимок ещё передаётся, ждём…', ''
            return self.finish(op, op['prefix'] + shot_failed(op['camera']), 'bad')
        if op['number']:
            return op['prefix'] + 'Камера сняла, «Нависет» забирает снимок…', ''
        return op['prefix'] + answer_text(7, 0, op['camera'], op['address']), ''

    def add_plate(self):
        """Форма ID «Автономер» с самым свежим номером (захват или журнал); передача — «Сохранить и синхронизировать»."""
        board = self.board()
        with board.lock:
            found = latest_plate(board.last_reader, board.journal)
        self.navigate('Доступ')
        self.edit_row('ids', preset={'identifier': found[1] if found else '', 'kind': 3})

    def open_camera(self):
        """С главной: камера номеров, если она есть в копии настроек; без копии — камера самого нового снимка."""
        raw = bytes.fromhex(self.store.get(self.imei).get('settings', '')) if self.imei else b''
        if table_addresses(raw, PLATE_TYPE) or table_addresses(raw, JPEG_TYPE):
            plate = bool(table_addresses(raw, PLATE_TYPE))
        else:
            plate = next((s['kind'] for s in self.shots if s['kind'] != 'unknown'), None) == 'plate'
        self.navigate('Камера номеров' if plate else 'JPEG-камера')

    def apply_camera_addresses(self, force=False):
        """Адреса камер по копии настроек. Адрес, введённый, но не отправленный, при правке ID и групп не затирается;
        force — после загрузки копии или «Сохранить и отправить настройки»: таблица 485 важнее."""
        data = self.store.get(self.imei)
        raw = bytes.fromhex(data.get('settings', ''))
        for tab, variable, key in (('JPEG-камера', self.jpeg_address, 'jpeg_address'),
                                   ('Камера номеров', self.plate_address, 'plate_address')):
            value, note = camera_address(raw, TAB_TYPES[tab], data.get(key))
            auto = self.auto_address.get(tab)
            if force or not auto or auto[0] != self.imei or variable.get() == auto[1]:
                variable.set(value)
            self.auto_address[tab] = self.imei, value
            self.address_hints[tab] = note
        self.address_warnings(raw)

    def address_warnings(self, raw):
        """Пояснение под полем адреса; если по таблице 485 этот адрес у другой камеры — предупредить."""
        for tab, variable in (('JPEG-камера', self.jpeg_address), ('Камера номеров', self.plate_address)):
            text, hint = variable.get().strip(), self.address_hints.get(tab, '')
            other = camera_of_address(raw, int(text)) if text.isascii() and text.isdecimal() and int(text) < 256 else None
            if other and other != CAMERA_TABS[tab]:
                hint = f'Внимание: по таблице 485 адрес {text} — это {CAMERAS[other]}, снимок сделает она'
            elif hint == FROM_TABLE and text not in map(str, table_addresses(raw, TAB_TYPES[tab])):
                hint = 'Адрес введён вручную'
            if self.address_notes[tab]['text'] != hint:
                self.address_notes[tab].configure(text=hint)

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
        # Адрес из таблицы 485 важнее сохранённого; старые camera_address/camera_format в данных не трогаем.
        self.apply_camera_addresses()
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

    def cause_records(self, board):
        """События-причины снимков из всей таблицы journal: в памяти платы только последние 1500,
        а объяснение старого снимка не должно меняться от перезапуска. Вызывать под board.lock."""
        if not board.queue:
            return list(board.journal)
        rows = board.queue.db.execute('SELECT raw FROM journal WHERE imei=?', (self.imei,))
        return [stand.Record(raw) for raw, in rows if len(raw) > 8 and raw[8] in CAUSES]

    def refresh_board(self):
        if not self.imei:
            self.dashboard.refresh(None)
            for panel in (self.jpeg_shots, self.plate_shots):
                panel.update_shots([], 'Выберите плату слева')
            return
        board = self.board()
        data = self.store.get(self.imei)
        self.heading.config(text=' · '.join(dict.fromkeys(filter(None, (data.get('name'), self.imei, data.get('object'))))))
        try:
            stamp = stand.PHOTOS.stat().st_mtime_ns  # новый файл в папке меняет время папки
        except OSError:
            stamp = None
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
                for record in reversed(board.journal[-150:]):
                    detail = record.arg_text() + (f', проходов {record.passes}' if record.passes != 255 and record.type in PASS_EVENTS else '')
                    if record.type == 129:
                        number = struct.unpack_from('<I', record.arg1)[0]
                        detail = f'снимок {local_when(number)} (№{number})'
                    self.events.insert('', 'end', values=(local_when(record.time), record.type, stand.EVENTS.get(record.type, '?'), detail))
                self.events.yview_moveto(position if self._event_signature and self._event_signature[0] == self.imei else 0)
                self._event_signature = signature
            # Список снимков пересчитывается вне замка и только когда что-то изменилось (раз в 30 с — на всякий случай).
            sent = tuple(r[0] for r in board.queue.db.execute(
                "SELECT sent FROM commands WHERE imei=? AND cmd=7 AND state='done' AND sent IS NOT NULL", (self.imei,))) if board.queue else ()
            shots_key = (self.imei, str(stand.PHOTOS), stamp, len(board.journal), board.journal[-1].index if board.journal else -1,
                         sent, int(time.time() // 30))
            causes = self.cause_records(board) if shots_key != self._shots_key else None
            busy = board.busy_photo
        if causes is not None:
            self.shots, self._shots_key = list_shots(stand.PHOTOS, self.imei, causes, sent), shots_key
        for panel in (self.jpeg_shots, self.plate_shots):
            panel.update_shots(self.shots)
            panel.set_busy(busy)
        copy = bytes.fromhex(data.get('settings', ''))
        settings = copy if len(copy) == 512 else bytes(512)
        self.address_warnings(copy)
        with board.lock:
            details = board.details
            for tab, label in (('JPEG-камера', self.jpeg_result), ('Камера номеров', self.plate_result)):
                text, tone = self.camera_result(tab, board)
                label.configure(text=text, foreground={'good': '#16734b', 'bad': '#b42318'}.get(tone, '#172b4d'))
            # Ответы устройств 485 — состояние периферии статуса 2; включённые — по сохранённой копии настроек.
            states = bytes.fromhex(details['peripherals']) if details and details.get('peripherals') else b''
            for i, (_, _, state) in enumerate(self.peripherals):
                state.set('—' if not settings[180+i*2] or len(states) < 8 else 'отвечает' if states[i] else 'нет ответа')
            for tab, label in self.camera_states.items():
                text = camera_state(details, copy, TAB_TYPES[tab], board.journal)
                if label['text'] != text:
                    label.configure(text=text)
            found = latest_plate(board.last_reader, board.journal)
            number, note = (found[1], f'{found[2]} · {local_when(int(found[0]))}') if found else ('—', 'Номер ещё не считывали')
            reader = board.last_reader
            if reader and reader[1] != 1 and (not found or reader[0] > found[0]):
                note += f'\nпоследний захват: время вышло · {local_when(int(reader[0]))}'
            self.plate_number.configure(text=number)
            self.plate_when.configure(text=note)
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
