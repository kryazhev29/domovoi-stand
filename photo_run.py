"""Сервер фото без окна (тот же Server из photo_server_gui.py) на 15010.
Держит плату на связи; каждый раз, когда рядом появляется файл capture.flag,
снимает камерой 1, ждёт бит 13 и забирает фото в Фото_с_камеры.
Запуск: python -u photo_run.py   (в фоне; снимок: создать capture.flag)."""
import queue, sys, time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
FLAG = SCRIPTS / 'capture.flag'
sys.path.insert(0, str(SCRIPTS))
import photo_server_gui as g

out = queue.Queue()
srv = g.Server(port=15010, base=SCRIPTS.parent, out=out)
srv.start(); srv.ready.wait(5)
if srv.error:
    sys.exit('server error: ' + srv.error)

state = {'peer': None, 'answer7': None}


def pump(t):
    end = time.monotonic() + t
    while time.monotonic() < end:
        try:
            kind, value = out.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == 'log':
            if ' RX cmd=2 ' in value or ' TX cmd=24 ' in value:
                continue  # статусы раз в 15 с
            print(value, flush=True)
            if 'ответ команды 7, код' in value:
                state['answer7'] = value.rsplit(' ', 1)[-1]
        elif kind == 'clients':
            state['peer'] = next((c for c in value if c['imei']), None)
        elif kind != 'progress':
            print(kind, value, flush=True)


def wait_for(cond, t):
    end = time.monotonic() + t
    while time.monotonic() < end and not cond():
        pump(1)
    return cond()


while True:
    pump(1)
    if not FLAG.exists():
        continue
    FLAG.unlink()
    if not state['peer']:
        print('no board connected', flush=True); continue
    key, imei = state['peer']['key'], state['peer']['imei']
    state['answer7'] = None
    print('>>> capture camera 1', flush=True)
    srv.command(key, 'capture', {'address': 1})
    ok = wait_for(lambda: state['peer'] and state['peer'].get('status', 0) & (1 << 13), 40)
    print(f'RESULT capture: bit13={bool(ok)} answer7={state["answer7"]}', flush=True)
    if not ok:
        continue
    print('>>> get photo', flush=True)
    srv.command(key, 'get', {})
    tr = lambda: srv.transfers.get(imei)
    wait_for(lambda: tr() is not None and not tr().active, 240)
    photos = sorted((SCRIPTS.parent / 'Фото_с_камеры').glob(imei + '_*.jpg'), key=lambda p: p.stat().st_mtime)
    print('RESULT photos:', [f'{p.name} {p.stat().st_size}' for p in photos[-3:]], flush=True)
