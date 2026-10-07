"""Встроенный просмотр только полностью сохранённых фотографий выбранной платы."""
import datetime as dt
import os
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = ImageOps = ImageTk = None


class PhotoPreview(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.source = self.image = self.path = self.signature = None
        self.resize_job = None
        self.canvas = tk.Canvas(self, height=270, background='#172b4d', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.caption = tk.StringVar(value='Фото пока нет')
        ttk.Label(self, textvariable=self.caption, style='Hint.TLabel').pack(anchor='w', pady=(6, 4))
        self.placeholder = 'Фото появится здесь после получения с платы'
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
            preview.thumbnail((max(1, width - 20), max(1, height - 20)), Image.Resampling.LANCZOS)
            self.image = ImageTk.PhotoImage(preview, master=self.canvas)
            self.canvas.create_image(width // 2, height // 2, image=self.image)
        else:
            self.image = None
            self.canvas.create_text(width // 2, height // 2, text=self.placeholder, fill='#d9e5f5',
                                    font=('Segoe UI', 11), width=max(100, width - 50), justify='center')

    def refresh(self, imei, folder):
        files = list(folder.glob(f'{imei}_*.jpg')) if imei else []
        try:
            latest = max(files, key=lambda path: path.stat().st_mtime_ns) if files else None
            stat = latest.stat() if latest else None
            signature = (imei, str(latest), stat.st_mtime_ns if stat else None, stat.st_size if stat else None)
        except OSError:
            return  # атомарная замена файла: повторим в следующем обновлении
        if signature == self.signature:
            return
        self.signature, self.path = signature, latest
        if self.source:
            self.source.close()
        self.source = None
        self.placeholder = 'Фото появится здесь после получения с платы'
        self.caption.set('Фото этой платы ещё не получено')
        if latest:
            received = dt.datetime.fromtimestamp(stat.st_mtime).astimezone().strftime('%d.%m.%Y %H:%M:%S')
            if Image is None:
                self.placeholder = 'Для просмотра JPEG нужен Pillow. Фото можно открыть кнопкой «Оригинал».'
            else:
                try:
                    with Image.open(latest) as original:
                        self.source = ImageOps.exif_transpose(original).convert('RGB')
                    self.caption.set(f'Получено {received} · {self.source.width} × {self.source.height} · двойной щелчок — оригинал')
                except (OSError, ValueError, Image.DecompressionBombError):
                    self.placeholder = 'Фото получено, но JPEG повреждён. Повторите снимок.'
                    self.caption.set(f'Получено {received} · не удалось показать JPEG')
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
