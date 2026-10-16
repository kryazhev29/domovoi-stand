# Доработка: Кряжев А., 2026
"""Тестовый сервер фото ДОМОВОЙ NEXT. SLDN совместим с fw_server.py; обновление FW не выполняется."""
import argparse
import datetime as dt
import os
import queue
import selectors
import socket
import struct
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from rs485_config import Settings485

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def build(cmd, payload=b'', uniq=0):
    body = bytes([60, uniq, cmd]) + payload
    length = struct.pack('<H', len(body))
    return b'SLDN' + length + body + struct.pack('<H', crc16(length + body))


def take_frames(buf):
    # Границы TCP произвольны; CRC и длина проверяются до обработки команды.
    out = []
    while True:
        at = buf.find(b'SLDN')
        if at < 0:
            return out, buf[-3:]
        buf = buf[at:]
        if len(buf) < 6:
            return out, buf
        size = int.from_bytes(buf[4:6], 'little')
        if not 3 <= size <= 2040:
            buf = buf[1:]; continue
        if len(buf) < size + 8:
            return out, buf
        if crc16(buf[4:size + 6]) != int.from_bytes(buf[size + 6:size + 8], 'little'):
            buf = buf[1:]; continue
        out.append((buf[7], buf[8], bytes(buf[9:size + 6])))
        buf = buf[size + 8:]


def valid_jpeg(data):
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValueError('JPEG не целый: нет FFD8/FFD9')
    if Image is not None:
        import io
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format != 'JPEG':
                raise ValueError('Это не JPEG')
            picture.load()


class Transfer:
    """Курсор сервера движется только после проверенного блока. Завершение — после записи JPEG."""
    def __init__(self, imei, directory, emit, faults=None):
        self.imei, self.directory, self.emit = imei, Path(directory), emit
        self.faults = faults or {}
        self.active, self.number, self.size = True, None, 0
        self.data, self.request = bytearray(), struct.pack('<IH', 0, 0)
        self.blocks = self.retries = self.packets = 0
        self.started = time.monotonic()
        self.cut_done = False
        self.awaiting_end = False
        self.resuming = False
        self.repeat_sent = False
        self.new_stream()

    def partial_path(self, number):
        # Принятая часть фото переживает перезапуск сервера: подтверждения плата хранит во Flash.
        return self.directory / f'{self.imei}_{number}.partial'

    def progress(self):
        elapsed = max(time.monotonic() - self.started, 0.001)
        self.emit('progress', dict(imei=self.imei, number=self.number, size=self.size,
                  received=len(self.data), blocks=self.blocks, speed=len(self.data) / elapsed,
                  retries=self.retries, active=self.active))

    def new_stream(self):
        self.sent, self.first = self.request, None

    def receive(self, payload, current=True):
        if not self.active:
            return None
        if current and self.first is None and len(payload) >= 11:
            self.first = struct.unpack_from('<I', payload, 1)[0], struct.unpack_from('<H', payload, 9)[0]
        self.packets += 1
        every = self.faults.get('drop', 0)
        if every and self.packets % every == 0:
            self.emit('log', f'{self.imei}: искусственно потерян пакет 14 №{self.packets}')
            return 'lost'
        if payload == b'\0':
            if self.resuming:
                return 'stale'
            if self.data and len(self.data) != self.size:
                self.request = struct.pack('<IH', self.number, len(self.data))
                self.resuming = True
                return 'resume'
            if self.data and not self.awaiting_end:
                self.request = struct.pack('<IH', self.number, self.size)
                self.awaiting_end = True
                return 'confirm'
            self.active = False; self.progress()
            self.emit('log', f'{self.imei}: очередь пуста, выгрузка завершена')
            return 'done'
        if len(payload) < 12:
            raise ValueError('Короткий пакет 14')
        count, number, size, offset = struct.unpack_from('<BIIH', payload)
        block = payload[11:]
        if not count or not 0 < size <= 65535 or not 0 < len(block) <= 1024 or offset + len(block) > size:
            raise ValueError('Неверные границы пакета 14')
        if self.number != number:
            if self.data and len(self.data) != self.size:
                if self.resuming:
                    return 'stale'
                self.request = struct.pack('<IH', self.number, len(self.data))
                self.resuming = True
                return 'resume'
            data = bytearray()
            if offset:
                # Подтверждённое начало плата хранит во Flash и повторно не отдаёт: берём его с диска.
                partial = self.partial_path(number)
                data = bytearray(partial.read_bytes()) if partial.exists() else bytearray()
                if len(data) < offset:
                    have = struct.pack('<IH', number, len(data))
                    if self.sent == have and self.first == (number, offset):
                        # Плата на прямой запрос начала отвечает дальше — начало у неё подтверждено.
                        self.active = False; self.progress()
                        self.emit('log', f'{self.imei}: фото {number}: первые {offset} байт подтверждены платой, '
                                         f'а у сервера есть только {len(data)} — выгрузка остановлена')
                        return 'abort'
                    if self.resuming:
                        return 'stale'
                    self.request = have
                    self.resuming = True
                    return 'resume'
                # Плата продолжает ровно с подтверждённого места, хвост после него придёт заново.
                del data[offset:]
                partial.write_bytes(data)
                self.emit('log', f'{self.imei}: фото {number}: продолжение с {offset} байт из {partial.name}')
            else:
                self.partial_path(number).unlink(missing_ok=True)
            self.number, self.size, self.data = number, size, data
            self.blocks, self.started = 0, time.monotonic()
            self.awaiting_end = False
        if size != self.size:
            raise ValueError('Размер того же фото изменился')
        if offset > len(self.data):
            if self.resuming:
                return 'stale'
            self.request = struct.pack('<IH', number, len(self.data))
            self.resuming = True
            return 'resume'
        if offset < len(self.data):
            if self.data[offset:offset + len(block)] != block:
                raise ValueError('Повтор блока отличается от уже принятых данных')
            # Старый дубль не откатывает текущий запрос и не пишет файл повторно.
            self.progress(); return 'duplicate'
        self.resuming = False
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.partial_path(number).open('ab') as stream:
            stream.write(block); stream.flush(); os.fsync(stream.fileno())
        self.data.extend(block); self.blocks += 1
        if len(self.data) == self.size:
            valid_jpeg(self.data)
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f'{self.imei}_{number}.jpg'
            temp = path.with_suffix('.jpg.part')
            with temp.open('wb') as stream:
                stream.write(self.data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
            self.partial_path(number).unlink(missing_ok=True)
            self.emit('image', str(path))
            self.emit('log', f'{self.imei}: сохранено {path.name}, {size} байт, JPEG проверен')
        self.request = struct.pack('<IH', number, len(self.data))
        self.progress()
        if self.faults.get('disconnect') and not self.cut_done and len(self.data) >= self.size / 2:
            self.cut_done = True
            return 'disconnect'
        return 'block'


class Peer:
    def __init__(self, conn, address, key):
        self.conn, self.address, self.key = conn, address, key
        self.imei, self.status, self.last_status = '', 0, ''
        self.rx, self.tx = b'', bytearray()
        self.uniq = 0
        self.transfer, self.due, self.deadline = None, None, None
        self.last_command_id = None
        self.errors = 0


class Server(threading.Thread):
    daemon = True

    def __init__(self, port=15010, host='0.0.0.0', base=None, out=None, retry=5.0):
        super().__init__()
        self.host, self.port, self.retry = host, port, retry
        self.base = Path(base) if base else Path(__file__).resolve().parents[1]
        self.out, self.actions = out if out is not None else queue.Queue(), queue.Queue()
        self.stop_event, self.ready = threading.Event(), threading.Event()
        self.peers, self.transfers = {}, {}
        self.selector = None
        self.sequence = 0
        self.error = None
        self.log_stream = None

    def emit(self, kind, value):
        if kind == 'log':
            value = f'{dt.datetime.now():%Y-%m-%d %H:%M:%S.%f}'[:-3] + '  ' + value
            if self.log_stream:
                self.log_stream.write(value + '\n'); self.log_stream.flush()
        self.out.put((kind, value))

    def clients(self):
        self.emit('clients', [dict(key=p.key, imei=p.imei, ip=f'{p.address[0]}:{p.address[1]}',
                    last=p.last_status, status=p.status) for p in self.peers.values()])

    def send(self, peer, command, payload=b''):
        peer.uniq = (peer.uniq + 1) & 255
        peer.tx.extend(build(command, payload, peer.uniq))
        self.selector.modify(peer.conn, selectors.EVENT_READ | selectors.EVENT_WRITE, peer)
        self.emit('log', f'{peer.imei or peer.address[0]} TX cmd={command} id={peer.uniq} {payload.hex(" ")}')
        return peer.uniq

    def schedule(self, peer, retry=False):
        tr = peer.transfer
        if not tr or not tr.active:
            return
        if retry:
            tr.retries += 1; tr.progress()
        peer.due = time.monotonic() + float(tr.faults.get('delay', 0))
        peer.deadline = None

    def disconnect(self, peer, reason):
        self.emit('log', f'{peer.imei or peer.address[0]}: отключение — {reason}')
        try:
            self.selector.unregister(peer.conn)
        except (KeyError, ValueError):
            pass
        peer.conn.close(); self.peers.pop(peer.key, None); self.clients()

    def handle(self, peer, uniq, cmd, payload):
        self.emit('log', f'{peer.imei or peer.address[0]} RX cmd={cmd} id={uniq} bytes={len(payload)}' +
                  (f' фото/размер/смещение={struct.unpack_from("<IIH", payload, 1)}' if cmd == 14 and len(payload) >= 11 else ''))
        if cmd == 1:
            if len(payload) < 17:
                raise ValueError('Короткая регистрация')
            imei = payload[:15].decode('ascii')
            if len(imei) != 15 or not imei.isdecimal() or not imei.isascii():
                raise ValueError('Некорректный IMEI')
            for other in list(self.peers.values()):
                if other is not peer and other.imei == imei:
                    self.disconnect(other, 'новое соединение того же IMEI')
            peer.imei = imei
            self.send(peer, 1, b'\0' + struct.pack('<I', int(time.time())))
            peer.transfer = self.transfers.get(imei)
            if peer.transfer and peer.transfer.active:
                self.schedule(peer, retry=True)
            self.clients()
        elif cmd == 2:
            if len(payload) < 6:
                raise ValueError('Короткий статус')
            peer.status = struct.unpack_from('<H', payload, 4)[0]
            peer.last_status = time.strftime('%H:%M:%S')
            self.send(peer, 24, bytes([uniq])); self.clients()
            if peer.status & (1 << 11) and peer.transfer:
                peer.transfer.active = False; peer.due = peer.deadline = None
                self.emit('log', f'{peer.imei}: стоп выгрузки, бит 11 установлен')
        elif cmd == 12 and len(payload) >= 7 and payload[0] == 4:
            self.emit('log', f'{peer.imei}: настройки приняты, ответ 12 (таймштамп/CRC): {payload.hex()}')
            self.emit('settings_result', f'{peer.imei}: настройки приняты (ответ 12)')
        elif cmd == 11 and len(payload) >= 2:
            self.emit('log', f'{peer.imei}: ответ команды {payload[0]}, код {payload[1]}')
            if payload[0] == 12:
                self.emit('settings_result', f'{peer.imei}: ответ 11 на команду 12, код {payload[1]}')
            if payload[0] == 21 and payload[1] and peer.transfer and peer.transfer.active:
                if uniq == peer.last_command_id:
                    if payload[1] == 1:
                        peer.transfer.request = struct.pack('<IH', 0, 0)
                        peer.transfer.resuming = False
                        self.emit('log', f'{peer.imei}: код 1, повтор команды 21 с 0/0')
                    self.schedule(peer, retry=True)
                    if payload[1] == 2:
                        # Плата занята съёмкой или восстановлением сектора: не засыпать её командами 21.
                        peer.due = time.monotonic() + self.retry
        elif cmd == 14 and peer.transfer and peer.transfer.active:
            result = peer.transfer.receive(payload, uniq == peer.last_command_id)
            if result == 'disconnect':
                self.disconnect(peer, 'искусственный разрыв на середине фото')
            elif result in ('resume', 'confirm'):
                self.schedule(peer, retry=result == 'resume')
            elif result in ('done', 'abort'):
                peer.due = peer.deadline = None
            elif result in ('block', 'duplicate'):
                peer.deadline = time.monotonic() + self.retry
            # Потерянный пакет не меняет таймаут; по нему повторим команду 21.

    def command(self, key, action, options=None):
        self.actions.put((key, action, options or {}))

    def do_actions(self):
        while True:
            try:
                key, action, options = self.actions.get_nowait()
            except queue.Empty:
                return
            peer = self.peers.get(key)
            if not peer or not peer.imei:
                self.emit('log', 'Контроллер не зарегистрирован'); continue
            if action == 'stop':
                if peer.transfer:
                    peer.transfer.active = False; peer.transfer.progress()
                peer.due = peer.deadline = None
                self.emit('log', f'{peer.imei}: остановлено пользователем'); continue
            if peer.status & (1 << 11):
                self.emit('log', f'{peer.imei}: команда запрещена при бите 11'); continue
            if action == 'settings485':
                self.send(peer, 12, options['config'])
            elif action == 'capture':
                address = int(options.get('address', 1))
                if not 0 <= address <= 255:
                    self.emit('log', 'Адрес камеры: 0–255'); continue
                self.send(peer, 7, bytes([address]))
            elif action == 'get':
                if int(options.get('drop', 0)) < 0 or float(options.get('delay', 0)) < 0:
                    self.emit('log', 'Параметры помех не могут быть отрицательными'); continue
                peer.transfer = Transfer(peer.imei, self.base / 'Фото_с_камеры', self.emit, options)
                self.transfers[peer.imei] = peer.transfer
                self.schedule(peer)

    def run(self):
        try:
            logs = self.base / 'Логи'; logs.mkdir(parents=True, exist_ok=True)
            self.log_stream = (logs / f'{dt.date.today():%Y-%m-%d}_photo_server.log').open('a', encoding='utf-8')
            with selectors.DefaultSelector() as selector, socket.socket() as listener:
                self.selector = selector
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.host, self.port)); self.port = listener.getsockname()[1]
                listener.listen(); listener.setblocking(False); selector.register(listener, selectors.EVENT_READ, None)
                self.emit('log', f'Слушаем {self.host}:{self.port}'); self.ready.set()
                while not self.stop_event.is_set():
                    self.do_actions()
                    for item, events in selector.select(0.05):
                        peer = item.data
                        if peer is None:
                            conn, address = listener.accept(); conn.setblocking(False); self.sequence += 1
                            peer = Peer(conn, address, str(self.sequence)); self.peers[peer.key] = peer
                            selector.register(conn, selectors.EVENT_READ, peer); self.clients(); continue
                        try:
                            if events & selectors.EVENT_READ:
                                chunk = peer.conn.recv(4096)
                                if not chunk:
                                    self.disconnect(peer, 'TCP закрыт'); continue
                                peer.rx += chunk
                                frames, peer.rx = take_frames(peer.rx)
                                for frame in frames:
                                    self.handle(peer, *frame)
                                    if peer.key not in self.peers:
                                        break
                            if events & selectors.EVENT_WRITE and peer.key in self.peers:
                                size = peer.conn.send(peer.tx); del peer.tx[:size]
                                if not peer.tx:
                                    selector.modify(peer.conn, selectors.EVENT_READ, peer)
                        except BlockingIOError:
                            pass
                        except (OSError, ValueError, UnicodeError) as exc:
                            self.disconnect(peer, str(exc))
                    now = time.monotonic()
                    for peer in list(self.peers.values()):
                        tr = peer.transfer
                        if not tr or not tr.active:
                            continue
                        if peer.deadline is not None and now >= peer.deadline:
                            self.schedule(peer, retry=True)
                        if peer.due is not None and now >= peer.due:
                            peer.last_command_id = self.send(peer, 21, tr.request); tr.new_stream()
                            if tr.faults.get('repeat') and not tr.repeat_sent:
                                peer.last_command_id = self.send(peer, 21, tr.request)
                                tr.repeat_sent = True
                            peer.due, peer.deadline = None, now + self.retry
                for peer in list(self.peers.values()):
                    self.disconnect(peer, 'сервер остановлен')
        except Exception as exc:
            self.error = str(exc); self.emit('log', f'Ошибка сервера: {exc}')
        finally:
            self.ready.set()
            if self.log_stream:
                self.log_stream.close(); self.log_stream = None
            self.emit('stopped', self.error)


class App:
    def __init__(self, root, base=None, port=15010):
        self.root, self.base, self.server = root, base, None
        self.events, self.preview, self.last_image = queue.Queue(), None, None
        root.title('ДОМОВОЙ NEXT — фото C328'); root.geometry('1100x780')
        top = ttk.Frame(root, padding=8); top.pack(fill='x')
        self.port, self.camera = tk.StringVar(value=str(port)), tk.StringVar(value='1')
        ttk.Label(top, text='TCP порт').pack(side='left'); ttk.Entry(top, textvariable=self.port, width=7).pack(side='left', padx=5)
        self.start = ttk.Button(top, text='Старт сервера', command=self.toggle); self.start.pack(side='left', padx=5)
        ttk.Label(top, text='Адрес камеры').pack(side='left'); ttk.Entry(top, textvariable=self.camera, width=5).pack(side='left', padx=5)
        for title, action in [('Сделать снимок', 'capture'), ('Забрать фото', 'get'), ('Стоп', 'stop')]:
            ttk.Button(top, text=title, command=lambda a=action: self.action(a)).pack(side='left', padx=4)
        ttk.Button(top, text='Настройки 485', command=self.settings485).pack(side='left', padx=4)
        self.tree = ttk.Treeview(root, columns=('imei','ip','last','status','b11','b13'), show='headings', height=4)
        for key, title in [('imei','IMEI'),('ip','IP:порт'),('last','Последний статус'),('status','Статус 1'),('b11','Бит 11: обновление'),('b13','Бит 13: фото')]:
            self.tree.heading(key, text=title); self.tree.column(key, width=150)
        self.tree.pack(fill='x', padx=8)
        self.bits = ttk.Label(root, text='Выберите контроллер', padding=8); self.bits.pack(fill='x')
        self.tree.bind('<<TreeviewSelect>>', self.show_bits)
        faults = ttk.Frame(root, padding=8); faults.pack(fill='x')
        self.drop, self.delay = tk.StringVar(value='0'), tk.StringVar(value='0')
        self.repeat, self.cut = tk.BooleanVar(), tk.BooleanVar()
        for title, value in [('Терять каждый N-й пакет 14 (0 — выкл.)',self.drop),('Задержка ответа, с',self.delay)]:
            ttk.Label(faults, text=title).pack(side='left'); ttk.Entry(faults, textvariable=value, width=5).pack(side='left', padx=5)
        ttk.Checkbutton(faults, text='Повторять запрос', variable=self.repeat).pack(side='left')
        ttk.Checkbutton(faults, text='Разорвать на середине', variable=self.cut).pack(side='left')
        self.progress = ttk.Label(root, text='Фото не запрошено', padding=8); self.progress.pack(fill='x')
        self.bar = ttk.Progressbar(root, maximum=100); self.bar.pack(fill='x', padx=8)
        self.picture = ttk.Label(root, text='Готовое фото появится здесь', anchor='center'); self.picture.pack(fill='both', expand=True)
        ttk.Button(root, text='Открыть сохранённое фото', command=self.open_image).pack()
        logbox = ttk.Frame(root); logbox.pack(fill='both', padx=8, pady=8)
        self.log = tk.Text(logbox, height=10, wrap='word', state='disabled')
        scroll = ttk.Scrollbar(logbox, command=self.log.yview); self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.log.pack(fill='both', expand=True)
        self.states = {}; root.protocol('WM_DELETE_WINDOW', self.close); root.after(50, self.pump)

    def toggle(self):
        if self.server:
            self.server.stop_event.set(); return
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535: raise ValueError()
        except ValueError:
            messagebox.showerror('Порт', 'Введите порт 1–65535'); return
        self.server = Server(port, base=self.base, out=self.events); self.server.start()
        self.start.configure(text='Остановить сервер')

    def action(self, action):
        selected = self.tree.selection()
        if not self.server or not selected:
            messagebox.showinfo('Контроллер', 'Запустите сервер и выберите контроллер'); return
        try:
            options = dict(address=int(self.camera.get()), drop=int(self.drop.get()),
                           delay=float(self.delay.get().replace(',','.')), repeat=self.repeat.get(), disconnect=self.cut.get())
            if not 0 <= options['address'] <= 255 or options['drop'] < 0 or not 0 <= options['delay'] <= 3600: raise ValueError()
        except ValueError:
            messagebox.showerror('Параметры', 'Адрес 0–255; N ≥ 0; задержка 0–3600 с'); return
        self.server.command(selected[0], action, options)

    def settings485(self):
        selected = self.tree.selection()
        if not self.server or not selected:
            messagebox.showinfo('Контроллер', 'Запустите сервер и выберите контроллер'); return
        server, key = self.server, selected[0]
        try:
            Settings485(self.root, lambda data: server.command(key, 'settings485', {'config': data}),
                        endpoint=os.environ.get('DOMOVOI_SERVER', ''))
        except (OSError, ValueError) as exc:
            messagebox.showerror('Настройки 485', str(exc))

    def show_bits(self, event=None):
        selected = self.tree.selection()
        if selected and selected[0] in self.states:
            status = self.states[selected[0]]['status']
            self.bits.configure(text='Биты статуса 1: ' + '   '.join(f'{i}={int(bool(status & (1 << i)))}' for i in range(16)))

    def open_image(self):
        if self.last_image and os.name == 'nt':
            os.startfile(self.last_image)

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.log.configure(state='normal'); self.log.insert('end', value + '\n'); self.log.see('end')
                    if int(self.log.index('end-1c').split('.')[0]) > 2000: self.log.delete('1.0','200.0')
                    self.log.configure(state='disabled')
                elif kind == 'settings_result':
                    messagebox.showinfo('Настройки 485', value, parent=self.root)
                elif kind == 'clients':
                    self.states = {p['key']:p for p in value}
                    for key in self.tree.get_children():
                        if key not in self.states: self.tree.delete(key)
                    for p in value:
                        values=(p['imei'],p['ip'],p['last'],f"0x{p['status']:04X}",int(bool(p['status']&2048)),int(bool(p['status']&8192)))
                        if self.tree.exists(p['key']): self.tree.item(p['key'],values=values)
                        else: self.tree.insert('', 'end', iid=p['key'],values=values)
                    self.show_bits()
                elif kind == 'progress':
                    self.progress.configure(text=f"{value['imei']} • Фото {value['number']} • {value['received']}/{value['size']} байт • {value['blocks']} блоков • {value['speed']:.0f} Б/с • повторы {value['retries']}" + ('' if value['active'] else ' • стоп/готово'))
                    self.bar['value'] = 100*value['received']/max(1,value['size'])
                elif kind == 'image':
                    self.last_image = value
                    if Image is not None:
                        with Image.open(value) as im:
                            im.thumbnail((900,330)); self.preview = ImageTk.PhotoImage(im.copy())
                        self.picture.configure(image=self.preview, text='')
                    else:
                        self.picture.configure(text='JPEG сохранён: ' + value); self.open_image()
                elif kind == 'stopped':
                    self.server = None; self.start.configure(text='Старт сервера')
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def close(self):
        if self.server:
            self.server.stop_event.set(); self.server.join(timeout=2)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=15010)
    parser.add_argument('--base', type=Path, help='Каталог Стенд_Domovoi')
    args = parser.parse_args()
    root = tk.Tk(); App(root, args.base, args.port); root.mainloop()


if __name__ == '__main__':
    main()
