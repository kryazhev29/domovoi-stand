"""Пассивная запись линии RS-485 через свисток: свисток ничего не передаёт, только слушает.
Запуск: py -3.11 -I line_sniff.py <секунд> <файл без расширения> [скорость 115200] [COM14]
Пишет .txt (время от старта, байты строкой) и сырой .bin. Посылки делит по тишине 5 мс."""
import sys, time
import serial

sys.stdout.reconfigure(encoding='utf-8')
sec, out = float(sys.argv[1]), sys.argv[2]
baud = int(sys.argv[3]) if len(sys.argv) > 3 else 115200
com = sys.argv[4] if len(sys.argv) > 4 else 'COM14'
s = serial.Serial(com, baud, timeout=0.005)
t0 = time.perf_counter(); chunk = b''; start = None; last = None
with open(out + '.txt', 'w', encoding='utf-8') as txt, open(out + '.bin', 'wb') as raw:
    def flush():
        global chunk, start
        if chunk:
            txt.write('%9.3f %4d %r\n' % (start - t0, len(chunk), chunk)); txt.flush()
            raw.write(chunk); raw.flush(); chunk = b''
    while time.perf_counter() - t0 < sec:
        b = s.read(1)
        now = time.perf_counter()
        if b:
            k = s.in_waiting
            b += s.read(k) if k else b''
            if not chunk:
                start = now
            chunk += b; last = now
        elif chunk and now - last >= 0.005:
            flush()
    flush()
s.close()
