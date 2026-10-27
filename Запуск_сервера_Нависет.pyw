"""Запуск ручного стенда в окне, без консоли."""
from tkinter import messagebox

try:
    from naviset_server import main
    main()
except SystemExit as exc:  # занятый порт и прочие отказы main() — тоже в окно, а не молча
    if exc.code not in (None, 0):
        messagebox.showerror('Сервер ДОМОВОЙ не запущен', str(exc.code))
except Exception as exc:
    messagebox.showerror('Сервер ДОМОВОЙ не запущен', str(exc))
