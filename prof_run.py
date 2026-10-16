"""Один и тот же замер для любой сборки с пакетом 113 (prof_read.py).
  python prof_run.py <метка>     итог в Логи\\замер_<метка>_<дата>.txt
Плата уже на тестовом сервере и на связи. Этапы: покой 2 мин, 3000 ID (100 пакетов),
CRC всех блоков ×3, открытие по последнему ID ×10 и по отсутствующему ×10. После каждого - замер тактов."""
import datetime, os, subprocess, sys, time

tag = sys.argv[1]
path = f'../Логи/замер_{tag}_{datetime.date.today().isoformat()}.txt'
env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}


def run(*args):
    r = subprocess.run(['python', *args], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
    return r.returncode, r.stdout + r.stderr


def part(title, text):
    block = f'=== {title}\n{text.strip()}\n\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(block)
    print(block, flush=True)


open(path, 'w', encoding='utf-8').write(f'Замер {tag}, {time.strftime("%d.%m.%Y %H:%M")}\n\n')
run('prof_read.py')                      # выбросить накопленное с загрузки
time.sleep(120)
part('покой 2 мин', run('prof_read.py')[1])
for check in ('тяжёлая_база', 'тяжёлые_crc', 'тяжёлые_открытия'):
    t0 = time.monotonic()
    code, out = run('ctl.py', 'run', check)
    lines = [l for l in out.splitlines() if l.startswith(('ГОДНО', 'НЕ ГОДНО', 'ОШИБКА'))] or out.splitlines()[-3:]
    part(f'{check} ({time.monotonic() - t0:.0f} с, код {code})', '\n'.join(lines) + '\n' + run('prof_read.py')[1])
