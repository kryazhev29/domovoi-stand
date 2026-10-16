"""Управление работающим тестовым сервером ДОМОВОЙ NEXT (порт 127.0.0.1:15011).

python ctl.py state                 подключена ли плата, флаги статуса
python ctl.py run all               безопасные проверки; run full - все; run временные_id журнал
python ctl.py run обновление firmware=..\\Прошивки\\139.71.<сборка>.bin
python ctl.py journal 20            последние записи журнала платы
python ctl.py send 22               любая команда, hex-данные после номера
python ctl.py reload                перечитать checks.py без перезапуска сервера
python ctl.py show                  показать окно сервера
Код выхода: 0 - всё годно, 1 - есть НЕ ГОДНО или ошибка, 2 - сервер не запущен.
"""
import sys

from test_server import control


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        return 0
    lines = control(' '.join(sys.argv[1:]))
    if lines is None:
        print('тестовый сервер не запущен: ярлык «Тестовый сервер ДОМОВОЙ» или python test_server.py', file=sys.stderr)
        return 2
    print('\n'.join(lines))
    return 1 if any(l.startswith(('НЕ ГОДНО', 'ошибка')) for l in lines) else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
