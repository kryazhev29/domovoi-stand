"""Запуск ручного стенда в окне, без консоли."""
from tkinter import messagebox

try:
    from naviset_server import main
    main()
except Exception as exc:
    messagebox.showerror('Сервер ДОМОВОЙ не запущен', str(exc))
