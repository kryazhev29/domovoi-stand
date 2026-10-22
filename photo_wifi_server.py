"""Снимки по USB с доставкой на «Нависет»: фото забирает сервер своим каналом (мост или GSM).

Запуск: py -3.11 photo_wifi_server.py [снимков 10] [пауза после снимка, с 60] [ожидание фото, с 150]
Фото по USB не читается и не подтверждается: его должен забрать сервер (команда 21) и положить
в Фото_с_камеры\\<IMEI>_<номер>.jpg. Журнал USB 108/109 и статус 101 раз в 2 с —
Логи\\photo_wifi_server_<время>.txt/.bin/.json. Настройки, перезапуск и заливка не используются.
COM5 должен быть свободен (NextConfig закрыт). После 3 недоставленных фото подряд серия останавливается.
"""
import ast
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import struct
import sys
import time

import serial

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from protocol import frame, take_frames, parse_answer11

sys.stdout.reconfigure(encoding="utf-8")
STAND = SCRIPTS.parent
PHOTOS = STAND / "Фото_с_камеры"
IMEI = "867442070217472"
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 10
GAP = float(sys.argv[2]) if len(sys.argv) > 2 else 60
WAIT = float(sys.argv[3]) if len(sys.argv) > 3 else 150
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
prefix = STAND / "Логи" / f"photo_wifi_server_{stamp}"
started = time.monotonic()
result = {"started": stamp, "requested": COUNT, "gap": GAP, "wait": WAIT,
          "photos": [], "completed": False}
log = prefix.with_suffix(".txt").open("x", encoding="utf-8")
raw = prefix.with_suffix(".bin").open("xb")


def say(line):
    text = f"+{time.monotonic() - started:8.3f} {line}"
    log.write(text + "\n")
    log.flush()
    if line.startswith("[host]") or "TCPDIS" in line:
        print(text, flush=True)


# Из usb_watch.py берётся только разбор журнала и статуса, без открытия COM.
source = ast.parse((SCRIPTS / "usb_watch.py").read_text(encoding="utf-8-sig"))
nodes = [n for n in source.body
         if isinstance(n, ast.FunctionDef) and n.name in {"txt", "on_log", "on_status"}
         or isinstance(n, ast.Assign) and any(
             isinstance(t, ast.Name) and t.id in {"EVENTS", "AT_RESULT", "PHASES"} for t in n.targets)]
decoder = {"struct": struct, "say": say, "text_tail": {}, "last_status": None}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "usb_watch_decoder", "exec"), decoder)

buf = b""
pending = deque()
uniq = 140
next_status = float("inf")
port = serial.Serial("COM5", 115200, timeout=0.05, write_timeout=2)


def send(cmd, payload=b""):
    global uniq
    uniq = (uniq + 1) % 256
    packet = frame(cmd, payload, uniq)
    if port.write(packet) != len(packet):
        raise RuntimeError("Неполная отправка; повтора нет")
    return uniq


def receive_until(deadline):
    global buf, next_status
    while time.monotonic() < deadline:
        if time.monotonic() >= next_status:
            send(101)
            next_status = time.monotonic() + 2
        if pending:
            uid, cmd, payload = pending.popleft()
            if cmd == 109:
                decoder["on_log"](payload)
            elif cmd == 101:
                decoder["on_status"](bytes([255, uid, cmd]) + payload)
            else:
                yield uid, cmd, payload
            continue
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            raw.write(chunk)
            raw.flush()
            packets, buf = take_frames(buf + chunk)
            pending.extend(packets)


def observe(seconds):
    for _ in receive_until(time.monotonic() + seconds):
        pass


def answer(cmd, payload=b"", seconds=3):
    request = send(cmd, payload)
    for uid, received, body in receive_until(time.monotonic() + seconds):
        if uid == request and received == 11:
            return parse_answer11(body, cmd)[0]
    raise RuntimeError(f"Нет ответа на команду {cmd}; повтор не отправлен")


def server_photos():
    return {p.name for p in PHOTOS.glob(f"{IMEI}_*.jpg")}


def jpeg_size(data):
    """(ширина, высота) из SOF, None — не целый JPEG."""
    if data[:2] != b"\xff\xd8" or data.rstrip(b"\x00")[-2:] != b"\xff\xd9":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        mark, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= mark <= 0xC3:
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    return None


def shot(index):
    before = server_photos()
    begun = time.monotonic()
    code = answer(7, b"\x02")
    item = {"index": index, "command_at": round(begun - started, 3), "answer7": code}
    result["photos"].append(item)
    if code:
        say(f"[host] СНИМОК {index}: плата отказала, код {code}")
        return False
    say(f"[host] СНИМОК {index}: команда принята, жду фото на сервере до {WAIT:.0f} с")
    while time.monotonic() < begun + WAIT:
        observe(1)
        fresh = sorted(server_photos() - before)
        if fresh:
            path = PHOTOS / fresh[0]
            data = path.read_bytes()
            size = jpeg_size(data)
            item.update(file=path.name, bytes=len(data), jpeg=size is not None,
                        width_height=size, seconds=round(time.monotonic() - begun, 3))
            say(f"[host] СНИМОК {index}: на сервере {path.name}, {len(data)} Б, "
                f"{'JPEG %dx%d' % size if size else 'НЕ ЦЕЛЫЙ JPEG'}, через {item['seconds']} с")
            return size is not None
    item["seconds"] = None
    say(f"[host] СНИМОК {index}: за {WAIT:.0f} с фото на сервер НЕ пришло")
    return False


try:
    auth = send(102, os.environ.get("DOMOVOI_PIN", "4321").encode("ascii")[:4])
    for uid, cmd, payload in receive_until(time.monotonic() + 3):
        if uid == auth and cmd == 102:
            if payload != b"\x01":
                raise RuntimeError("ПИН не принят")
            break
    else:
        raise RuntimeError("Нет подтверждения ПИН")
    send(108, b"\x07")
    next_status = time.monotonic()
    say(f"[host] журнал включён; снимков {COUNT}, пауза {GAP:.0f} с")
    observe(5)
    misses = 0
    for index in range(1, COUNT + 1):
        misses = 0 if shot(index) else misses + 1
        if misses == 3:
            say("[host] 3 недоставленных фото подряд — серия остановлена")
            break
        observe(GAP)
    result["completed"] = misses < 3
except BaseException as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
    say(f"[host] ОШИБКА {result['error']}")
    raise
finally:
    try:
        send(108, b"\x00")
        observe(2)
        say("[host] журнал выключен; COM5 закрыт")
    finally:
        port.close()
        result["delivered"] = sum(1 for p in result["photos"] if p.get("jpeg"))
        result["elapsed"] = round(time.monotonic() - started, 3)
        prefix.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
        log.close()
        raw.close()
        print(f"RESULT {prefix.with_suffix('.json')}", flush=True)
