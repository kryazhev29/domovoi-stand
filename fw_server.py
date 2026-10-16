"""Test server for DOMOVOI NEXT: the board connects here over TCP and gets the firmware.
A window with three settings: port, .bin file, reply delay. Standard library only.
Usage: py -3.11 fw_server.py
Frame: 'SLDN' + len(2) + timeout(1) + uniqId(1) + cmd(1) + payload + crc16(2)."""
import queue, socket, struct, threading, time, tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk


def crc16(data):
    """CRC-16 ModBus, as crc16_calc() in the firmware."""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def crc_stm(data):
    """CRC32 of the STM32 unit: the value command 19 carries."""
    c = 0xFFFFFFFF
    for i in range(0, len(data), 4):
        c ^= struct.unpack_from('<I', data, i)[0]
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c


def build(cmd, payload, uniq):
    body = bytes([255, uniq, cmd]) + payload
    lb = struct.pack('<H', len(body))
    return b'SLDN' + lb + body + struct.pack('<H', crc16(lb + body))


def take_frames(buf):
    """Pull whole frames out of the stream. Returns (frames, leftover)."""
    out, p = [], 0
    while True:
        i = buf.find(b'SLDN', p)
        if i < 0:
            p = max(p, len(buf) - 3)
            break
        if len(buf) < i + 6:
            p = i
            break
        ln = struct.unpack_from('<H', buf, i + 4)[0]
        if ln < 3 or ln > 2048:
            p = i + 4
            continue
        if len(buf) < i + 8 + ln:
            p = i
            break
        got = struct.unpack_from('<H', buf, i + 6 + ln)[0]
        if got == crc16(buf[i + 4:i + 6 + ln]):
            out.append((buf[i + 7], buf[i + 8], bytes(buf[i + 9:i + 6 + ln])))
            p = i + 8 + ln
        else:
            p = i + 4
    return out, buf[p:]


class Image:
    def __init__(self, path):
        self.path = Path(path)
        hw, fw, stamp = (int(x) for x in self.path.name.split('.')[:3])
        body = self.path.read_bytes()
        body += b'\xff' * (-len(body) % 4)
        self.hw, self.fw, self.stamp, self.body = hw, fw, stamp, body
        self.blocks = (len(body) + 1023) // 1024
        self.crc = crc_stm(body)

    def cmd19(self):
        return struct.pack('<HIIIH', self.fw, self.stamp, len(self.body), self.crc, self.blocks)

    def block(self, n):
        return struct.pack('<H', n) + self.body[n * 1024:(n + 1) * 1024].ljust(1024, b'\xff')


class Server(threading.Thread):
    """One board at a time. Everything is said from the reader loop, so nothing needs locking."""
    daemon = True

    def __init__(self, port, out):
        super().__init__()
        self.port, self.out = port, out
        self.image = None          # set before the update button works
        self.delay = 0.0           # seconds a block waits before it goes out
        self.send_update = False   # raised by the button
        self.stopping = False
        self.uniq = 0
        self.imei = ''
        self.sent = 0
        self.started = None

    def say(self, text, kind='info'):
        self.out.put((kind, text))

    def next_uniq(self):
        self.uniq = (self.uniq + 1) & 255
        return self.uniq

    def run(self):
        try:
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('0.0.0.0', self.port))
            srv.listen(1)
            srv.settimeout(0.3)
        except OSError as e:
            self.say(f'порт {self.port} занять не удалось: {e}', 'bad')
            self.out.put(('stopped', ''))
            return
        self.say(f'слушаем порт {self.port}, ждём плату')
        while not self.stopping:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.say(f'плата подключилась с {addr[0]}:{addr[1]}', 'good')
            try:
                self.serve(conn)
            except OSError as e:
                self.say(f'связь оборвалась: {e}', 'bad')
            finally:
                conn.close()
                self.imei = ''
            self.say('плата отключилась')
        srv.close()
        self.out.put(('stopped', ''))

    def serve(self, conn):
        conn.settimeout(0.2)
        buf, due, pending = bytearray(), None, None
        while not self.stopping:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            except socket.timeout:
                pass
            frames, buf = take_frames(buf)
            for uniq, cmd, payload in frames:
                asked = self.handle(conn, uniq, cmd, payload)
                if asked is not None:
                    pending, due = asked, time.monotonic() + self.delay
            if self.send_update:
                self.send_update = False
                self.start_update(conn)
            if pending is not None and time.monotonic() >= due:
                n, pending = pending, None
                conn.sendall(build(20, self.image.block(n), self.next_uniq()))
                self.sent += 1
                self.say(f'блок {n} отдан')

    def handle(self, conn, uniq, cmd, payload):
        """Answers at once; returns a block number when one has to be sent afterwards."""
        if cmd == 1:                                   # регистрация
            self.imei = payload[:15].decode('ascii', 'replace').strip('\x00')
            self.say(f'регистрация: IMEI {self.imei}, протокол {payload[15]}, железо {payload[16]}')
            conn.sendall(build(1, bytes([0]) + struct.pack('<I', int(time.time())), self.next_uniq()))
        elif cmd == 2:                                 # статус 1
            st = struct.unpack_from('<H', payload, 4)[0]
            self.say(f'статус {st:#06x}' + (' (бит 11: идёт обновление)' if st & 0x0800 else ''))
            conn.sendall(build(24, bytes([uniq]), self.next_uniq()))
        elif cmd == 4:                                 # ход обновления
            pct, code = payload[0], payload[1]
            if code == 0:
                self.say(f'блок принят, {pct}%')
            elif code == 253:
                self.say(f'ПЛАТА ПРЕКРАТИЛА ЗАГРУЗКУ (253) на {pct}%', 'bad')
            else:
                self.say(f'блок отвергнут, код {code}, {pct}%', 'bad')
        elif cmd == 11:                                # подтверждение нашей команды
            self.say(f'ответ на команду {payload[0]}: код {payload[1]}')
        elif cmd == 13:                                # запрос блока
            n = struct.unpack_from('<H', payload, 0)[0]
            if self.image is None:
                self.say(f'запрошен блок {n}, но обновление не начато', 'bad')
                return None
            self.say(f'запрошен блок {n}')
            return n
        else:
            self.say(f'команда {cmd} ({len(payload)} байт)')
        return None

    def start_update(self, conn):
        self.sent, self.started = 0, time.monotonic()
        conn.sendall(build(19, self.image.cmd19(), self.next_uniq()))
        i = self.image
        self.say(f'команда 19: версия {i.fw}, сборка {i.stamp}, {len(i.body)} байт, '
                 f'{i.blocks} блоков, CRC {i.crc:#010x}', 'good')


class App:
    def __init__(self, root):
        self.root, self.out, self.srv = root, queue.Queue(), None
        root.title('Тестовый сервер ДОМОВОЙ NEXT')
        root.geometry('900x560')

        top = ttk.Frame(root, padding=8)
        top.pack(fill='x')
        ttk.Label(top, text='Порт').grid(row=0, column=0, sticky='w')
        self.port = ttk.Entry(top, width=8)
        self.port.insert(0, '15010')
        self.port.grid(row=0, column=1, padx=(4, 16), sticky='w')
        ttk.Label(top, text='Задержка блока, с').grid(row=0, column=2, sticky='w')
        self.delay = ttk.Entry(top, width=8)
        self.delay.insert(0, '0')
        self.delay.grid(row=0, column=3, padx=(4, 16), sticky='w')
        self.btn_run = ttk.Button(top, text='Старт сервера', command=self.toggle)
        self.btn_run.grid(row=0, column=4, sticky='w')

        ttk.Label(top, text='Прошивка').grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.file = ttk.Entry(top)
        self.file.grid(row=1, column=1, columnspan=3, sticky='we', padx=4, pady=(8, 0))
        ttk.Button(top, text='Обзор…', command=self.pick).grid(row=1, column=4, pady=(8, 0), sticky='w')
        self.btn_upd = ttk.Button(top, text='Отправить обновление', command=self.update, state='disabled')
        self.btn_upd.grid(row=1, column=5, padx=(8, 0), pady=(8, 0))
        top.columnconfigure(3, weight=1)

        self.state = ttk.Label(root, padding=(8, 2))
        self.state.pack(fill='x')
        box = ttk.Frame(root, padding=8)
        box.pack(fill='both', expand=True)
        bar = ttk.Scrollbar(box)
        bar.pack(side='right', fill='y')
        self.log = tk.Text(box, wrap='none', font=('Consolas', 9), bg='#101010',
                           fg='#d0d0d0', yscrollcommand=bar.set)
        self.log.pack(side='left', fill='both', expand=True)
        bar.configure(command=self.log.yview)
        self.log.tag_config('good', foreground='#7ec46b')
        self.log.tag_config('bad', foreground='#e06c6c')
        self.show_state()
        root.after(100, self.pump)

    @staticmethod
    def local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'
        finally:
            s.close()

    def write(self, kind, text):
        self.log.insert('end', f'{time.strftime("%H:%M:%S")}  {text}\n',
                        kind if kind in ('good', 'bad') else '')
        self.log.see('end')

    def pick(self):
        p = filedialog.askopenfilename(title='Файл прошивки HW.FW.СБОРКА.bin',
                                       initialdir=str(Path(__file__).resolve().parent.parent / 'Прошивки'),
                                       filetypes=[('Прошивка', '*.bin')])
        if p:
            self.file.delete(0, 'end')
            self.file.insert(0, p)

    def toggle(self):
        if self.srv:
            self.srv.stopping = True
            self.btn_run.configure(state='disabled', text='Останавливаю…')
            return
        try:
            port = int(self.port.get())
        except ValueError:
            self.write('bad', 'порт числом')
            return
        self.srv = Server(port, self.out)
        self.srv.start()
        self.btn_run.configure(text='Стоп')
        self.btn_upd.configure(state='normal')

    def update(self):
        if not self.srv:
            return
        try:
            image = Image(self.file.get())
        except Exception as e:
            self.write('bad', f'файл не читается: {e}')
            return
        try:
            self.srv.delay = float(self.delay.get().replace(',', '.'))
        except ValueError:
            self.write('bad', 'задержка числом')
            return
        self.srv.image = image
        self.srv.send_update = True

    def pump(self):
        while True:
            try:
                kind, text = self.out.get_nowait()
            except queue.Empty:
                break
            if kind == 'stopped':
                self.srv = None
                self.btn_run.configure(state='normal', text='Старт сервера')
                self.btn_upd.configure(state='disabled')
            else:
                self.write(kind, text)
        self.show_state()
        self.root.after(100, self.pump)

    def show_state(self):
        s = self.srv
        if not s:
            self.state.configure(text=f'Сервер остановлен.   Адрес для платы: {self.local_ip()}:{self.port.get()}')
            return
        parts = [f'IMEI {s.imei}' if s.imei else 'плата не подключена']
        if s.image and s.started:
            sec = time.monotonic() - s.started
            parts.append(f'отдано блоков {s.sent} из {s.image.blocks}')
            parts.append(f'{int(sec // 60)}:{int(sec % 60):02d}')
            if s.sent:
                parts.append(f'{sec / s.sent:.1f} с на блок')
        self.state.configure(text='   |   '.join(parts))


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
