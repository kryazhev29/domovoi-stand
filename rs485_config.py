# Доработка: Кряжев А., 2026
"""Редактор копии config_s. Никаких COM-портов и сетевых операций."""
import configparser
import struct
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

DEFAULT_FILE = Path(__file__).resolve().parents[1] / 'Логи/2026-09-21_плата1_конфиг_до_своего_сервера.bin'
FIELDS = [('Режим 485', 172, 'mode'), ('Скорость 485', 174, 'baud')]
for i in range(8):
    FIELDS += [(f'Устройство {i+1}: адрес', 179+i*2, None), (f'Устройство {i+1}: тип', 180+i*2, 'types')]
FIELDS += [('Тип Wiegand', 178, 'wiegand')]

def crc16(data):
    crc = 0xffff
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return crc

def read_config(path):
    data = Path(path).read_bytes()
    if len(data) != 512 or crc16(data[:510]) != int.from_bytes(data[510:], 'little'):
        raise ValueError('Нужен файл read_config.py: 512 байт config_s с верной внутренней CRC16')
    return data

def prepare_config(original, values, endpoint):
    if len(original) != 512:
        raise ValueError('Размер config_s должен быть 512 байт')
    host, sep, port = endpoint.strip().rpartition(':')
    if not sep or not host or host in ('0.0.0.0', '127.0.0.1', 'localhost') or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('Укажите доступный плате адрес нашего сервера: узел:порт')
    address = endpoint.strip().encode('ascii')
    if len(address) > 31:
        raise ValueError('Адрес сервера не длиннее 31 байта')
    if original[219] != 1:
        raise ValueError('В исходном файле сервер 1 выключен: выберите актуальную USB-копию настроек')
    result = bytearray(original)
    allowed = {offset for _, offset, _ in FIELDS}
    for offset, value in values.items():
        if offset not in allowed or not 0 <= value <= 255:
            raise ValueError('Неверное поле 485 или значение вне 0–255')
        result[offset] = value
    result[8:40] = address.ljust(32, b'\0')
    result[510:] = struct.pack('<H', crc16(result[:510]))
    return bytes(result)

def load_names(path):
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding='utf-8-sig'):
        raise ValueError(f'Не найден periphery.ini: {path}')
    names = {'mode': {0: 'Пассивный', 1: 'Активный'}}
    for section in ('types', 'wiegand', 'baud'):
        names[section] = {int(k): v for k, v in parser.items(section) if k.isdecimal() and 0 <= int(k) <= 255}
    return names

def differences(old, new, names):
    lines = []
    for title, offset, section in FIELDS:
        if old[offset] != new[offset]:
            label = lambda v: names.get(section, {}).get(v, str(v))
            lines.append(f'{title}: {label(old[offset])} → {label(new[offset])}')
    if old[8:40] != new[8:40]:
        text = lambda b: b.split(b'\0')[0].decode('ascii', 'replace')
        lines.append(f'Сервер 1: {text(old[8:40])} → {text(new[8:40])}')
    return lines

class Settings485:
    def __init__(self, parent, send, endpoint='', ini=None):
        self.send = send
        self.window = tk.Toplevel(parent)
        self.window.title('Настройки 485 — команда 12')
        self.window.transient(parent)
        self.path = tk.StringVar(value=str(DEFAULT_FILE))
        self.endpoint = tk.StringVar(value=endpoint)
        self.fields = {}
        self.names = load_names(ini or Path(__file__).with_name('periphery.ini'))
        ttk.Label(self.window, text='Основа: USB-копия config_s; «было» относится к файлу, а не к текущей плате.').grid(row=0, columnspan=3, padx=10, pady=5)
        ttk.Entry(self.window, textvariable=self.path, width=80).grid(row=1, columnspan=2, sticky='ew')
        ttk.Button(self.window, text='Открыть…', command=self.browse).grid(row=1, column=2)
        ttk.Label(self.window, text='Текущий адрес нашего сервера').grid(row=2, column=0)
        ttk.Entry(self.window, textvariable=self.endpoint, width=40).grid(row=2, column=1, sticky='w')
        for row, (title, offset, section) in enumerate(FIELDS, 3):
            ttk.Label(self.window, text=title).grid(row=row, column=0, sticky='w', padx=10)
            if section:
                control = ttk.Combobox(self.window, state='readonly', width=42)
                control.bind('<MouseWheel>', lambda event: 'break')
                control.bind('<<ComboboxSelected>>', lambda event: self.warning())
            else:
                control = ttk.Spinbox(self.window, from_=0, to=255, width=8)
            control.grid(row=row, column=1, sticky='w', pady=1)
            self.fields[offset] = control
        self.note = ttk.Label(self.window, text='')
        self.note.grid(row=22, columnspan=3, pady=5)
        ttk.Button(self.window, text='Проверить изменения…', command=self.submit).grid(row=23, columnspan=3, pady=10)
        try:
            self.load()
        except (OSError, ValueError) as exc:
            self.original = None
            self.note.config(text=str(exc))

    def browse(self):
        path = filedialog.askopenfilename(parent=self.window, filetypes=[('USB config_s', '*.bin')])
        if path:
            self.path.set(path)
            try:
                self.load()
            except (OSError, ValueError) as exc:
                self.original = None
                messagebox.showerror('Настройки', str(exc), parent=self.window)

    def load(self):
        self.original = read_config(self.path.get())
        for _, offset, section in FIELDS:
            control, value = self.fields[offset], self.original[offset]
            if section:
                mapping = dict(self.names[section])
                mapping.setdefault(value, str(value))
                control.codes = list(mapping)
                control['values'] = [f'{code}: {mapping[code]}' for code in control.codes]
                control.current(control.codes.index(value))
            else:
                control.set(str(value))
        self.warning()

    def values(self):
        values = {}
        for _, offset, section in FIELDS:
            control = self.fields[offset]
            if section:
                if control.current() < 0:
                    raise ValueError('Выберите значение в списке')
                values[offset] = control.codes[control.current()]
            else:
                values[offset] = int(control.get())
        return values

    def warning(self):
        try:
            values = self.values()
        except (ValueError, AttributeError):
            return
        types = [values[180+i*2] for i in range(8)]
        self.note.config(text='JPEG-камера C328 должна быть на шине одна' if 9 in types and sum(t != 0 for t in types) > 1 else '')
        for i in range(8):
            self.fields[179+i*2].config(state='normal' if values[172] else 'disabled')
            self.fields[180+i*2].config(state='readonly' if values[172] else 'disabled')

    def submit(self):
        try:
            if self.original is None:
                raise ValueError('Сначала загрузите USB-копию настроек')
            data = prepare_config(self.original, self.values(), self.endpoint.get())
            changes = differences(self.original, data, self.names)
            if not changes:
                messagebox.showinfo('Настройки 485', 'Изменений нет', parent=self.window)
                return
            if messagebox.askokcancel('Было → станет', '\n'.join(changes) + '\n\nОтправить команду 12 выбранному контроллеру?', parent=self.window):
                self.send(data)
                self.window.destroy()
        except (ValueError, UnicodeError) as exc:
            messagebox.showerror('Настройки 485', str(exc), parent=self.window)
