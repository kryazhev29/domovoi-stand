"""Испорченные кадры на пути плата -> сервер по сырым кускам TCP «Нависета».

Запуск: py -3.11 photo_frame_errors.py <с ЧЧ:ММ:СС> <по ЧЧ:ММ:СС> [дата ГГГГ-ММ-ДД, по умолчанию сегодня]
Время местное (UTC+7), как в журналах стенда. Читает Логи\\adapter\\traffic\\<IMEI>_<дата>.jsonl,
собирает принятые куски (kind=chunk) в один поток, ищет кадры SLDN и проверяет их CRC16.
Печатает по видам кадров: всего и с плохим CRC, и сколько испорченных короче своей длины (выпали байты).
Ничего не пишет и с платой не связывается.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")
IMEI = "867442070217472"
LOCAL = timezone(timedelta(hours=7))
TRAFFIC = Path(__file__).resolve().parent.parent / "Логи" / "adapter" / "traffic"


def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


day = sys.argv[3] if len(sys.argv) > 3 else datetime.now(LOCAL).strftime("%Y-%m-%d")
lo, hi = (datetime.fromisoformat(f"{day}T{t}").replace(tzinfo=LOCAL).astimezone(timezone.utc) for t in sys.argv[1:3])
raw = b""
for line in (TRAFFIC / f"{IMEI}_{day}.jsonl").open(encoding="utf-8", errors="replace"):
    line = line.strip().strip("\x00")
    if not line:
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue  # хвост файла после сбоя питания ПК
    if r["direction"] == "RX" and r["kind"] == "chunk" and lo <= datetime.fromisoformat(r["utc"]) < hi:
        raw += bytes.fromhex(r["hex"])

marks, i = [], 0
while (j := raw.find(b"SLDN", i)) >= 0:
    marks.append(j)
    i = j + 4
total, bad, short = Counter(), Counter(), 0
for k, j in enumerate(marks):
    if j + 9 > len(raw):
        continue
    n = struct.unpack_from("<H", raw, j + 4)[0]
    size = 6 + n + 2
    kind = (raw[j + 8], "большой" if size > 100 else "малый")
    total[kind] += 1
    if j + size > len(raw) or crc16(raw[j + 4:j + 6 + n]) != struct.unpack_from("<H", raw, j + 6 + n)[0]:
        bad[kind] += 1
        short += (marks[k + 1] if k + 1 < len(marks) else len(raw)) - j < size
print(f"{day} {sys.argv[1]}–{sys.argv[2]}: принято {len(raw)} Б, кадров {len(marks)}")
for kind in sorted(total):
    share = f" ({100 * bad[kind] / total[kind]:.1f} %)" if bad[kind] else ""
    print(f"  команда {kind[0]:3} {kind[1]:8} всего {total[kind]:4}, испорчено {bad[kind]}{share}")
print(f"  испорченных короче своей длины: {short} из {sum(bad.values())}")
