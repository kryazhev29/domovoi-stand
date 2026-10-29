"""Снимки платы в окне: список одной камеры и кадр выбранного файла.

Что сняла какая камера и почему — naviset_shots (без tkinter: его берёт и сервер).
"""
import os
import subprocess
import tkinter as tk
from tkinter import ttk

from naviset_shots import AUTO_NOTE, CAMERA_TABS

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = ImageOps = ImageTk = None


def fit_text(label):
    """Перенос подписи по её настоящей ширине: без обрезки и без одинокого слова на 100 и 125 %."""
    label.bind('<Configure>', lambda event: label.configure(wraplength=max(200, event.width - 4)), add='+')
    return label


class PhotoPreview(ttk.Frame):
    """Кадр выбранного файла; двойной щелчок открывает оригинал."""
    def __init__(self, parent, placeholder='Выберите снимок в списке'):
        super().__init__(parent)
        self.source = self.image = self.path = self.signature = None
        self.resize_job = None
        self.empty = self.placeholder = placeholder
        self.canvas = tk.Canvas(self, height=270, background='#172b4d', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.caption = tk.StringVar(value='')
        ttk.Label(self, textvariable=self.caption, style='Hint.TLabel').pack(anchor='w', pady=(6, 0))
        self.canvas.bind('<Configure>', self.resize)
        self.canvas.bind('<Double-Button-1>', lambda event: self.open())

    def resize(self, event=None):
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(60, self.draw)

    def draw(self):
        self.resize_job = None
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        self.canvas.delete('all')
        if self.source:
            preview = self.source.copy()
            preview.thumbnail((max(1, width - 16), max(1, height - 16)), Image.Resampling.LANCZOS)
            self.image = ImageTk.PhotoImage(preview, master=self.canvas)
            self.canvas.create_image(width // 2, height // 2, image=self.image)
        else:
            self.image = None
            self.canvas.create_text(width // 2, height // 2, text=self.placeholder, fill='#d9e5f5',
                                    font=('Segoe UI', 11), width=max(100, width - 50), justify='center')

    def show(self, path):
        """Показать файл (None — пусто). Тот же файл без изменений не перечитывается."""
        try:
            stat = path.stat() if path else None
        except OSError:
            return                    # атомарная замена файла: повторим в следующем обновлении
        signature = (str(path), stat and stat.st_mtime_ns, stat and stat.st_size)
        if signature == self.signature:
            return
        self.signature, self.path = signature, path
        if self.source:
            self.source.close()
        self.source = None
        self.placeholder, text = self.empty, ''
        if path:
            text = f'{path.name} · {stat.st_size / 1024:.0f} КБ'
            if Image is None:
                self.placeholder = 'Для просмотра JPEG нужен Pillow. Снимок можно открыть кнопкой «Оригинал».'
            else:
                try:
                    with Image.open(path) as original:
                        self.source = ImageOps.exif_transpose(original).convert('RGB')
                    text += f' · {self.source.width} × {self.source.height}'
                except (OSError, ValueError, Image.DecompressionBombError):
                    self.placeholder = 'Файл получен, но JPEG повреждён или неполный. Повторите снимок.'
                    text += ' · не удалось показать JPEG'
        self.caption.set(text)
        self.draw()

    def set_empty(self, text):
        """Подпись пустого кадра: «снимков пока нет» или «выберите снимок»."""
        if text != self.empty:
            self.empty = text
            if not self.path:
                self.placeholder = text
                self.draw()

    def open(self):
        if self.path:
            os.startfile(str(self.path))

    def destroy(self):
        if self.resize_job:
            self.after_cancel(self.resize_job)
        if self.source:
            self.source.close()
        super().destroy()




class ShotPanel(ttk.Frame):
    """Снимки одной камеры: список новых сверху и кадр выбранной строки рядом.
    Файлы, камеру которых не определить, видны на обеих вкладках с пометкой «камера?»."""
    def __init__(self, parent, app, kind, columns, folder):
        super().__init__(parent)
        self.kind, self.folder, self.shots, self.signature = kind, folder, {}, None
        self.columnconfigure(0, weight=3, uniform='shots')
        self.columnconfigure(1, weight=2, uniform='shots')
        self.rowconfigure(0, weight=1)
        holder = ttk.Frame(self)
        holder.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        self.list = app.tree(holder, columns, height=6)
        for key, *_ in columns:
            self.list.column(key, stretch=key in ('cause', 'reason'))
        self.list.tag_configure('unknown', foreground='#956000')
        self.list.bind('<<TreeviewSelect>>', lambda event: self.preview.show(self.selected()))
        self.list.bind('<Double-1>', lambda event: self.preview.open())
        side = ttk.Frame(self)
        side.grid(row=0, column=1, sticky='nsew')
        self.preview = PhotoPreview(side)
        self.preview.canvas.configure(height=120)  # растягивается по списку; подпись под кадром не обрезается
        self.preview.pack(fill='both', expand=True)
        actions = ttk.Frame(self)
        actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        ttk.Button(actions, text='Открыть папку снимков', command=self.open_folder).pack(side='left')
        self.count, self.busy = tk.StringVar(value=''), tk.StringVar(value='')
        ttk.Label(actions, textvariable=self.count, style='Hint.TLabel').pack(side='left', padx=(12, 0))
        ttk.Label(actions, textvariable=self.busy, style='Hint.TLabel', foreground='#2563eb').pack(side='left', padx=(8, 0))
        ttk.Button(actions, text='Оригинал ↗', command=self.preview.open).pack(side='right')
        self.note = fit_text(ttk.Label(self, style='Hint.TLabel', justify='left'))
        self.note.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 0))

    def selected(self):
        chosen = self.list.selection()
        return self.shots[chosen[0]]['path'] if chosen and chosen[0] in self.shots else None

    def set_busy(self, busy):
        """Идёт приём снимка — отдельной строкой, список не перестраивается."""
        self.busy.set('· принимаем снимок…' if busy else '')

    def summary(self, mine, shots):
        """Строка под списком: сколько снимков; пусто — где снимки другой камеры."""
        known = sum(s['kind'] != 'unknown' for s in mine)
        if mine:
            return f'Снимков: {known}' + (f' · камера не определена: {len(mine) - known} (отмечены «камера?»)'
                                          if known < len(mine) else '')
        other = 'plate' if self.kind == 'jpeg' else 'jpeg'
        count = sum(s['kind'] == other for s in shots)
        title = {'jpeg': 'JPEG-камеры', 'plate': 'камеры номеров'}
        return (f'Снимков {title[self.kind]} пока нет' +
                (f' · у {title[other]} {count} — вкладка «{CAMERA_TABS[other]}»' if count else ''))

    def update_shots(self, shots, empty=None):
        """Перерисовать, только если список изменился; выбор сохраняется, новый снимок встаёт сверху и выбирается.
        empty — подпись, когда плата не выбрана."""
        mine = [s for s in shots if s['kind'] in (self.kind, 'unknown')]
        folder = self.folder()
        count = empty or self.summary(mine, shots)
        signature = (tuple((s['name'], s['size'], s['cause']) for s in mine), count, str(folder))
        if signature == self.signature:
            return
        self.note.configure(text=AUTO_NOTE.format(folder=folder))
        self.count.set(count)
        self.preview.set_empty('Выберите снимок в списке' if mine else 'Снимков пока нет')
        children = self.list.get_children()
        chosen = self.list.selection()
        follow = not chosen or (children and chosen[0] == children[0])
        self.signature = signature
        self.list.delete(*children)
        self.shots = {s['name']: s for s in mine}
        for s in mine:
            values = {'time': s['time'], 'plate': s['plate'] or '—', 'reason': s['reason'], 'cause': s['cause'],
                      'size': f'{s["size"] / 1024:.0f} КБ'}
            text = 'reason' if 'reason' in self.list['columns'] else 'cause'
            if s['kind'] == 'unknown':
                values[text] = f'камера? · {values[text]}'
            self.list.insert('', 'end', iid=s['name'], values=[values[k] for k in self.list['columns']], tags=(s['kind'],))
        target = mine[0]['name'] if mine and follow else chosen[0] if chosen and chosen[0] in self.shots else None
        if target:
            self.list.selection_set(target)
            self.list.see(target)
        self.preview.show(self.shots[target]['path'] if target else None)

    def open_folder(self):
        """Папка снимков; выбранный файл в ней выделен."""
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        chosen = self.selected()
        if chosen and chosen.exists():
            subprocess.Popen(f'explorer /select,"{chosen}"')
        else:
            os.startfile(str(folder))
