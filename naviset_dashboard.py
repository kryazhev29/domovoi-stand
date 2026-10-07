"""Главный экран: подключение, безопасная диагностика и результаты по IMEI."""
import os
import json
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

from naviset_diagnostics import CHECKS, Diagnostics, save_report


LABELS = {'pass': 'Пройдено', 'fail': 'Ошибка', 'warn': 'Внимание', 'skip': 'Не проверено'}


def enable_dpi_awareness():
    # Иначе Windows растягивает готовую картинку Tk: шрифты размыты на 125–150%.
    if os.name == 'nt':
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def apply_theme(root):
    tkfont.nametofont('TkDefaultFont', root=root).configure(family='Segoe UI', size=10)
    tkfont.nametofont('TkTextFont', root=root).configure(family='Segoe UI', size=10)
    root.configure(bg='#f3f6fb')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background='#f3f6fb', foreground='#172b4d', font=('Segoe UI', 10))
    style.configure('TFrame', background='#f3f6fb')
    style.configure('TLabel', background='#f3f6fb')
    style.configure('Title.TLabel', font=('Segoe UI', 19, 'bold'))
    style.configure('Subtitle.TLabel', font=('Segoe UI', 12, 'bold'))
    style.configure('Hint.TLabel', foreground='#596b83')
    style.configure('TButton', padding=(12, 8), background='#e8eef8', borderwidth=0, relief='flat',
                    bordercolor='#e8eef8', lightcolor='#e8eef8', darkcolor='#e8eef8')
    style.map('TButton', background=[('active', '#e8eef8')], foreground=[('disabled', '#8c98ab')])
    style.configure('Accent.TButton', background='#2563eb', foreground='white', borderwidth=0)
    style.map('Accent.TButton', background=[('disabled', '#dde5f1'), ('active', '#1d4ed8')],
              foreground=[('disabled', '#8392a8')])
    style.configure('Side.TFrame', background='#14243c')
    style.configure('Side.TLabel', background='#14243c', foreground='#a5b8d2', font=('Segoe UI', 9, 'bold'))
    style.configure('Side.TButton', background='#223952', foreground='#eaf2ff')
    style.map('Side.TButton', background=[('active', '#304c70')], foreground=[('active', 'white')])
    style.configure('Nav.TButton', anchor='w', background='#14243c', foreground='#c8d5e7',
                    borderwidth=0, padding=(14, 10))
    style.map('Nav.TButton', background=[('active', '#223952')], foreground=[('active', 'white')])
    style.configure('Selected.Nav.TButton', background='#2563eb', foreground='white')
    style.map('Selected.Nav.TButton', background=[('active', '#3472f0')], foreground=[('active', 'white')])
    style.configure('Card.TFrame', background='white', relief='flat', borderwidth=0)
    style.configure('PlainCard.TFrame', background='white', relief='flat', borderwidth=0)
    style.configure('Card.TLabel', background='white')
    style.configure('Value.Card.TLabel', background='white', font=('Segoe UI', 15, 'bold'))
    style.configure('Good.Card.TLabel', background='white', foreground='#16734b', font=('Segoe UI', 15, 'bold'))
    style.configure('TLabelframe', background='#f3f6fb', bordercolor='#d8e1ed')
    style.configure('TLabelframe.Label', font=('Segoe UI', 10, 'bold'))
    style.configure('TEntry', padding=5, fieldbackground='white')
    style.configure('Horizontal.TProgressbar', background='#2563eb', troughcolor='#e2eaf6',
                    bordercolor='#e2eaf6', lightcolor='#2563eb', darkcolor='#2563eb', thickness=5)
    style.configure('Treeview', rowheight=30, background='white', fieldbackground='white', borderwidth=0)
    style.configure('Treeview.Heading', background='#eaf0f8', foreground='#53657b', padding=(8, 7), relief='flat')
    style.map('Treeview', background=[('selected', '#dce9ff')], foreground=[('selected', '#153e80')])
    style.configure('Desk.TNotebook', borderwidth=0, background='#f3f6fb',
                    bordercolor='#f3f6fb', lightcolor='#f3f6fb', darkcolor='#f3f6fb')
    style.layout('Desk.TNotebook.Tab', [])
    style.configure('TNotebook', borderwidth=0, background='#f3f6fb', bordercolor='#e2eaf6')
    style.configure('TNotebook.Tab', padding=(14, 9), background='#e8eef8', borderwidth=0)
    style.map('TNotebook.Tab', background=[('selected', 'white'), ('active', '#dce8fa')],
              foreground=[('selected', '#1747a6')])
    return style


class Dashboard:
    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.reports, self.runners = {}, {}
        self.rows = {}
        self.displayed = None
        ttk.Label(parent, text='Проверка платы', style='Title.TLabel').pack(anchor='w')
        ttk.Label(parent, text='Подключите плату → запустите проверку → сохраните результат.',
                  style='Hint.TLabel').pack(anchor='w', pady=(4, 16))
        cards = ttk.Frame(parent)
        cards.pack(fill='x')
        self.cards = {}
        for i, (key, title) in enumerate((('link', 'СВЯЗЬ'), ('firmware', 'ПРОШИВКА'), ('signal', 'СИГНАЛ GSM'))):
            cards.columnconfigure(i, weight=1, uniform='card')
            box = ttk.Frame(cards, style='Card.TFrame', padding=14)
            box.grid(row=0, column=i, sticky='nsew', padx=(0, 8 if i < 2 else 0))
            ttk.Label(box, text=title, style='Card.TLabel').pack(anchor='w')
            value = ttk.Label(box, text='—', style='Value.Card.TLabel')
            value.pack(anchor='w', pady=(9, 3))
            note = ttk.Label(box, text='Ожидание платы', style='Card.TLabel', foreground='#64748b', wraplength=int(185*app.ui_scale))
            note.pack(anchor='w')
            self.cards[key] = value, note
        self.connection_note = tk.StringVar(value='Подключённая плата появится слева автоматически.')
        ttk.Label(parent, textvariable=self.connection_note, style='Hint.TLabel', wraplength=int(680*app.ui_scale)).pack(anchor='w', pady=12)
        action = ttk.Frame(parent)
        action.pack(fill='x', pady=(4, 8))
        self.start_button = ttk.Button(action, text='Проверить плату', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(action, text='Остановить', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        self.report_button = ttk.Button(action, text='Открыть отчёт', command=self.open_report, state='disabled')
        self.report_button.pack(side='right')
        self.progress = ttk.Progressbar(parent, maximum=len(CHECKS))
        self.progress.pack(fill='x', pady=(4, 10))
        self.summary = tk.StringVar(value='Проверка ещё не запускалась')
        ttk.Label(parent, textvariable=self.summary, style='Subtitle.TLabel', wraplength=int(690*app.ui_scale)).pack(anchor='w', pady=(0, 8))
        table = ttk.Frame(parent, style='Card.TFrame', padding=(14, 4))
        table.pack(fill='x')
        for key, title in CHECKS:
            row = ttk.Frame(table, style='PlainCard.TFrame', padding=(0, 9))
            row.pack(fill='x')
            line = ttk.Frame(row, style='PlainCard.TFrame')
            line.pack(fill='x')
            ttk.Label(line, text=title, style='Card.TLabel', font=('Segoe UI', 10, 'bold')).pack(side='left')
            state = ttk.Label(line, text='Не проверено', style='Card.TLabel')
            state.pack(side='right')
            detail = ttk.Label(row, text='Запустите проверку', style='Card.TLabel', foreground='#64748b', wraplength=int(680*app.ui_scale))
            detail.pack(anchor='w', pady=(3, 0))
            self.rows[key] = state, detail
        ttk.Label(parent, text='Проверка читает статус, версию, время и CRC. Реле, камера, звонки и SMS проверяются отдельно.',
                  style='Hint.TLabel', wraplength=700).pack(anchor='w', pady=12)
        manual = ttk.Frame(parent)
        manual.pack(fill='x')
        for title, page in (('Реле и считыватель →', 'Контроллер'), ('Камера и события →', 'Фото и журнал')):
            ttk.Button(manual, text=title, command=lambda p=page: app.navigate(p)).pack(side='left', padx=(0, 8))

    def render(self, imei):
        self.displayed = imei
        if imei and imei not in self.reports:
            files = sorted((self.app.server.state_dir / 'reports').glob(f'проверка_{imei}_*.json'), reverse=True)
            if files:
                try:
                    record = json.loads(files[0].read_text(encoding='utf-8'))
                    if record.get('imei') == imei and len(record.get('results', [])) == len(CHECKS):
                        if all(r.get('key') in dict(CHECKS) and r.get('state') in LABELS and isinstance(r.get('detail'), str)
                               for r in record['results']):
                            record['previous'] = True
                            if files[0].with_suffix('.txt').exists():
                                record['path'] = str(files[0].with_suffix('.txt'))
                            self.reports[imei] = record
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
        record = self.reports.get(imei, {})
        results = {r['key']: r for r in record.get('results', [])}
        for key, (state, detail) in self.rows.items():
            result = results.get(key)
            state.configure(text=LABELS[result['state']] if result else 'Не проверено',
                            foreground={'pass': '#16734b', 'fail': '#b42318', 'warn': '#956000'}.get(result and result['state'], '#64748b'))
            detail.configure(text=result['detail'] if result else 'Запустите проверку')
        self.progress['value'] = len(results)
        self.report_button.configure(state='normal' if record.get('path') else 'disabled')
        if imei in self.runners:
            self.summary.set(f'Проверка выполняется · {len(results)} из {len(CHECKS)}')
        elif record.get('finished'):
            outcome = 'Связь и ответы проверены' if record.get('passed') else 'Проверка требует внимания'
            self.summary.set(outcome + ' · ' + record['finished'][11:19])
        elif record.get('error'):
            self.summary.set(record['error'])
        else:
            self.summary.set('Проверка ещё не запускалась')

    def refresh(self, board):
        imei = self.app.imei
        if imei != self.displayed:
            self.render(imei)
        online = bool(board and board.conn and board.reg)
        age = time.perf_counter() - board.status[3] if board and board.status else None
        fresh = online and age is not None and age < board.profile['heartbeat_timeout']
        link, link_note = self.cards['link']
        link.configure(text='На связи' if fresh else 'Ожидание статуса' if online else 'Нет связи',
                       style='Good.Card.TLabel' if fresh else 'Value.Card.TLabel')
        link_note.configure(text=f'Статус {age:.0f} с назад' if fresh else 'Подключите контроллер')
        details = board.details if online else None
        value, note = self.cards['firmware']
        value.configure(text=f'FW {details["firmware"]}' if details else '—')
        note.configure(text=f'Сборка {details["build"]}' if details else 'Прочитаем при проверке')
        value, note = self.cards['signal']
        signal = details['signal'] if details else None
        value.configure(text=f'{signal} / 31' if signal is not None and 0 <= signal <= 31 else '—')
        note.configure(text='По последнему запросу статуса' if details else 'Прочитаем при проверке')
        self.connection_note.set(f'IMEI {imei} · сеанс {board.sessions}' if online else
                                 f'Сервер слушает порт {self.app.server.port}. Подключите плату к этому компьютеру; она появится в списке слева.')
        busy = imei in self.runners
        self.start_button.configure(state='normal' if fresh and not busy else 'disabled')
        self.stop_button.configure(state='normal' if busy else 'disabled')
        record = self.reports.get(imei, {})
        if record.get('finished') and board and (not online or record.get('previous') or record.get('session') != board.sessions) and not busy:
            self.summary.set('Результат прошлого подключения · повторите проверку')

    def start(self):
        board = self.app.board()
        imei = board.imei
        if imei in self.runners:
            return
        with board.lock:
            if not board.conn or not board.reg or not board.status:
                raise ValueError('Дождитесь подключения платы и первого статуса')
            if board.queue.flags & 0x800 or board.sync_plan or board.queue.rows():
                raise ValueError('Выполняются команды. Дождитесь окончания; состояние — в разделе «Очередь».')
        runner = Diagnostics(board, lambda result: self.app.messages.put(('diagnostic', (imei, 'row', result))))
        self.runners[imei] = runner
        self.reports[imei] = {'results': [], 'session': board.sessions}
        self.render(imei)
        self.refresh(board)
        def work():
            try:
                report = runner.run()
                report['path'] = str(save_report(report, self.app.server.state_dir / 'reports'))
                self.app.messages.put(('diagnostic', (imei, 'done', report)))
            except Exception as exc:
                self.app.messages.put(('diagnostic', (imei, 'error', str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def receive(self, payload):
        imei, kind, value = payload
        record = self.reports.setdefault(imei, {'results': []})
        if kind == 'row':
            record['results'] = [r for r in record['results'] if r['key'] != value['key']] + [value]
        else:
            self.runners.pop(imei, None)
            if kind == 'done':
                self.reports[imei] = value
                self.app.notice.set(f'Отчёт сохранён · {value["path"]}')
            else:
                record['error'] = 'Проверка прервана: ' + value
                self.app.notice.set(record['error'])
        if self.app.imei == imei:
            self.render(imei)

    def stop(self):
        runner = self.runners.get(self.app.imei)
        if runner:
            runner.cancel.set()
            self.summary.set('Останавливаем проверку…')

    def open_report(self):
        path = self.reports.get(self.app.imei, {}).get('path')
        if path:
            os.startfile(path)
