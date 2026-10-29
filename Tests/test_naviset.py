"""Регрессии Нависет: локальная модель, отдельная БД, без реального оборудования."""
import gc
import json
import math
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_server as stand
from fake_board import FakeBoard
from naviset_diagnostics import Diagnostics, save_report
from naviset_queue import CommandQueue
from naviset_server import Board, Control, Server, read_profile, show_existing


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.queue = CommandQueue(clock=lambda: self.now)
        self.queue.bind('123456789012345')
        self.queue.free = 2048
        self.addCleanup(self.queue.db.close)

    def test_sent_command_timeout_does_not_claim_it_was_not_executed(self):
        self.queue.enqueue(8, bytes([3, 1, 0]), ttl=5)
        self.queue.flush(lambda *args: None)
        self.now += 6
        self.queue.expire()
        self.assertEqual(self.queue.history()[0]['state'], 'unknown')

    def test_unsent_command_expiry(self):
        self.queue.enqueue(8, bytes([3, 1, 0]), ttl=5, delay=10)
        self.now += 6
        self.queue.expire()
        self.assertEqual(self.queue.history()[0]['state'], 'expired')

    def test_disconnect_marks_only_sent_unknown(self):
        self.queue.enqueue(8, bytes([3, 1, 0]))
        self.queue.enqueue(11, b'SMS', delay=10)
        self.queue.flush(lambda *args: None)
        self.queue.disconnect()
        self.assertFalse(self.queue.online)
        self.assertEqual([r['state'] for r in self.queue.history()], ['unknown', 'queued'])

    def test_invalid_nonfinite_delay_and_ttl(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.queue.enqueue(22, delay=value)
                with self.assertRaises(ValueError):
                    self.queue.enqueue(22, ttl=value)

    def test_history_is_bounded_and_chronological(self):
        for i in range(180):
            uid = self.queue.enqueue(22, bytes([i]))
            self.queue.finish(next(r for r in self.queue.rows() if r['uid'] == uid))
        history = self.queue.history()
        self.assertEqual(len(history), 150)
        self.assertEqual(history[0]['id'], 31)
        self.assertEqual(history[-1]['id'], 180)

    def test_photo_finishes_only_after_end_of_stream(self):
        uid = self.queue.enqueue(21, bytes(6))
        self.queue.flush(lambda *args: None)
        self.queue.observe(uid, 11, bytes([21, 0]))
        self.assertEqual(self.queue.history()[0]['state'], 'sent')
        self.queue.observe(uid, 14, b'\1' + bytes(12))
        self.assertEqual(self.queue.history()[0]['state'], 'sent')
        self.queue.observe((uid+3) % 256, 14, b'\0')
        self.assertEqual(self.queue.history()[0]['state'], 'done')


class RecordingBoard(FakeBoard):
    def __init__(self, port):
        super().__init__(port=port)
        self.received = []
        self.behavior = ''
        self.shot = None  # JPEG, который отдаст команда 7 (иначе — заглушка имитатора, «камера не определена»)

    def command(self, uid, cmd, data):
        self.received.append(cmd)
        if cmd == 22 and self.behavior == 'reject':
            self.answer(cmd, uid, 2)
        elif cmd == 22 and self.behavior == 'silent':
            return
        elif cmd == 7 and self.behavior == 'refuse7':
            self.answer(cmd, uid, 2)
        else:
            super().command(uid, cmd, data)
            if cmd == 7 and self.shot:
                self.photo = self.shot


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='naviset-test-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.old_logs, self.old_photos = stand.LOGS, stand.PHOTOS
        stand.LOGS, stand.PHOTOS = self.folder, self.folder / 'photos'
        self.messages = queue.Queue()
        self.server = Server(0, lambda text, kind='info': self.messages.put((kind, text)), '127.0.0.1',
                             state_dir=self.folder / 'state')
        self.server.start()
        self.assertTrue(self.server.ready.wait(3))
        self.assertIsNone(self.server.error)
        self.fake = RecordingBoard(self.server.port)
        self.worker = threading.Thread(target=self.fake.run, daemon=True)
        self.worker.start()
        self.board = self.server.board
        self.assertTrue(self.board.until(lambda: self.board.reg and self.board.status, 3))
        self.addCleanup(self.cleanup_server)
        self.assertTrue(self.board.until(lambda: not self.board.queue.rows(), 3))
        self.fake.received.clear()

    def cleanup_server(self):
        self.fake.stop.set()
        self.worker.join(2)
        self.server.stop()
        self.server.join(2)
        for board in self.server.boards.values():
            if board.queue:
                board.queue.db.close()
        stand.LOGS, stand.PHOTOS = self.old_logs, self.old_photos

    def test_diagnostic_real_tcp_only_reads_and_writes_report(self):
        report = Diagnostics(self.board, timeout=2).run()
        self.assertTrue(report['passed'], report)
        self.assertEqual(len(report['results']), 4)
        self.assertTrue(set(self.fake.received) <= {1, 24, 22, 17}, self.fake.received)
        self.assertIn(22, self.fake.received)
        self.assertIn(17, self.fake.received)
        destination = save_report(report, self.folder / 'reports')
        self.assertIn(self.board.imei, destination.read_text(encoding='utf-8-sig'))
        self.assertEqual(json.loads(destination.with_suffix('.json').read_text(encoding='utf-8'))['passed'], True)

    def test_reject_is_not_a_pass(self):
        self.fake.behavior = 'reject'
        report = Diagnostics(self.board, timeout=1).run()
        self.assertFalse(report['passed'])
        rows = {r['key']: r for r in report['results']}
        self.assertEqual(rows['status']['state'], 'fail')
        self.assertEqual(rows['clock']['state'], 'skip')
        self.assertEqual(rows['crc']['state'], 'pass')

    def test_photo_transfers_over_tcp_and_leaves_queue_idle(self):
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (640, 480), '#164c7e').save(buffer, format='JPEG')
        self.fake.photo = buffer.getvalue()
        self.fake.photo_number = 1791000000
        self.board.send(21, bytes(6))
        self.assertTrue(self.board.until(lambda: 1791000000 in self.board.completed_photos and not self.board.queue.rows()
                                        and not self.board.busy_photo, 4))
        path = stand.PHOTOS / f'{self.board.imei}_1791000000.jpg'
        self.assertEqual(path.read_bytes(), buffer.getvalue())
        self.assertTrue(Diagnostics(self.board, timeout=2).run()['passed'])

    def test_timeout_finishes_and_crc_still_runs(self):
        self.fake.behavior = 'silent'
        report = Diagnostics(self.board, timeout=1).run()
        self.assertFalse(report['passed'])
        self.assertEqual(report['results'][1]['state'], 'fail')
        self.assertEqual(report['results'][-1]['state'], 'pass')

    def test_cancel_releases_diagnostic_lock(self):
        self.fake.behavior = 'silent'
        runner = Diagnostics(self.board, timeout=2)
        cancel = threading.Timer(.15, runner.cancel.set)
        cancel.start()
        self.addCleanup(cancel.cancel)
        report = runner.run()
        self.assertFalse(report['passed'])
        self.assertFalse(self.board.check_lock.locked())
        self.assertEqual(report['results'][-1]['state'], 'skip')
        self.assertNotIn(17, self.fake.received)

    def test_disconnection_during_check_is_not_pass(self):
        self.fake.behavior = 'silent'
        timer = threading.Timer(.15, self.fake.stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        report = Diagnostics(self.board, timeout=2).run()
        self.assertFalse(report['passed'])
        self.assertTrue(any('Связь прервалась' in row['detail'] for row in report['results']))

    def test_no_diagnostics_during_pending_commands(self):
        self.board.send(22, delay=4)
        report = Diagnostics(self.board, timeout=1).run()
        self.assertFalse(report['passed'])
        self.assertTrue(all(r['state'] == 'skip' for r in report['results']))

    def test_new_session_clears_old_firmware_details(self):
        self.board.details = {'build': 123}
        with self.board.lock:
            self.board.identify(self.board.imei)
        self.assertIsNone(self.board.details)

    def test_reopen_uses_existing_server_only(self):
        shown = threading.Event()
        control = Control(self.server, {}, lambda *args: None, shown.set, port=0)
        control.start()
        self.addCleanup(control.sock.close)
        port = control.sock.getsockname()[1]
        self.assertTrue(show_existing(port, self.server.host, self.server.port, self.server.state_dir))
        self.assertTrue(shown.wait(1))
        with self.assertRaises(ValueError):
            show_existing(port, self.server.host, self.server.port + 1, self.server.state_dir)

    def test_old_server_is_not_accepted_as_naviset(self):
        with patch('naviset_server.socket.create_connection') as connect:
            stream = connect.return_value.__enter__.return_value.makefile.return_value.__enter__.return_value
            stream.readline.return_value = 'команды старого сервера\n'
            self.assertFalse(show_existing(15011, '0.0.0.0', 15010, self.folder))


class ProfileTests(unittest.TestCase):
    def test_nonfinite_profile_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'profile.json'
            for name in ('heartbeat_timeout', 'command_delay', 'time_drift_limit'):
                path.write_text(json.dumps({name: math.nan}), encoding='utf-8')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    read_profile(path)


class DataTests(unittest.TestCase):
    def test_plate_is_stored_like_the_number_camera_sends_it(self):
        from naviset_data import id_record, plate_latin
        self.assertEqual(plate_latin(' а001хн197 '), 'A001XH197')
        self.assertEqual(plate_latin('c020pm177'), 'C020PM177')
        for bad in ('', 'А 001', 'Д001ДД77', 'A001XH19777'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                plate_latin(bad)
        row = dict(cell=5, identifier='С020РМ177', group=1, kind=3, expiry='', minutes=0, daily=255, remaining=255)
        self.assertEqual(id_record(row)[2:12], b'C020PM177\x00')
        row.update(kind=1, identifier='9031234567')
        self.assertEqual(id_record(row)[2:12], b'9031234567')

    def test_periphery_list_without_ini_matches_stand_ini(self):
        import naviset_ui
        with patch('naviset_ui.load_names', side_effect=ValueError('нет файла')):
            types = naviset_ui.periphery_types()
        self.assertEqual(types, naviset_ui.PERIPHERY)
        self.assertEqual(sorted(types), [0, 1, 8, 9, 16, 24])


def close_window(app):
    """Без вопроса окна и без отложенного обновления в следующем тесте. Сборка мусора — здесь, в главном
    потоке: иначе переменные Tk может собрать поток сервера («main thread is not in main loop»)."""
    app._closing = True
    for pending in app.root.tk.call('after', 'info'):
        app.root.after_cancel(pending)
    app.root.destroy()
    gc.collect()


class WindowTests(unittest.TestCase):
    setUp = NetworkTests.setUp
    cleanup_server = NetworkTests.cleanup_server

    def test_rs485_table_status_and_number_camera(self):
        from naviset_ui import App, periphery_choice
        app = App(self.server, self.messages, self.server.say)
        app.root.withdraw()

        self.addCleanup(close_window, app)
        app.root.update()
        app.select()
        imei = self.board.imei
        raw = bytearray(512)
        raw[510:] = struct.pack('<H', stand.crc16(raw[:510]))
        data = app.store.get(imei)
        data['settings'] = raw.hex()
        app.store.put(imei, data)
        app.load_data()
        self.assertEqual(app.peripherals[0][0].get(), periphery_choice(0))
        # Камера номеров по адресу 48: пассивный режим, повтор адреса и адрес 0 не уходят на плату.
        app.peripherals[0][0].set(periphery_choice(8))
        app.peripherals[0][1].set('48')
        with self.assertRaisesRegex(ValueError, 'Активный опрос'):
            app.save_settings()
        app.settings_fields['mode485'].set('Активный опрос')
        app.peripherals[1][0].set(periphery_choice(9))
        app.peripherals[1][1].set('48')
        with self.assertRaisesRegex(ValueError, 'занят'):
            app.save_settings()
        app.peripherals[1][1].set('0')
        with self.assertRaisesRegex(ValueError, 'резерв'):
            app.save_settings()
        app.peripherals[1][0].set(periphery_choice(0))
        app.plate_address.set('')
        app.save_settings()
        self.assertTrue(self.board.until(lambda: self.fake.tables[12][172] == 1 and self.fake.tables[12][179:183] == bytes([48, 8, 0, 0]), 4))
        # Новая строка 485 видна во вкладках камер сразу после отправки, без смены платы.
        self.assertEqual(app.plate_address.get(), '48')
        self.assertIn('нет строки «9 — фотокамера JPEG»', app.jpeg_note['text'])
        # Адрес камеры номеров берётся из таблицы 485, даже если раньше сохранили другой; JPEG-камеры в таблице нет — поле пустое.
        data = app.store.get(imei)
        data.update(plate_address=5, camera_address=1, camera_format='Протокол2')
        app.store.put(imei, data)
        app.load_data()
        self.assertEqual(app.plate_address.get(), '48')
        self.assertEqual(app.jpeg_address.get(), '')
        # Введённый, но не отправленный адрес правка ID не затирает; адрес камеры номеров на JPEG-вкладке — предупреждение.
        app.jpeg_address.set('48')
        app.load_data()
        self.assertEqual(app.jpeg_address.get(), '48')
        app.refresh_board()
        self.assertIn('это камера номеров', app.jpeg_note['text'])
        app.remember('jpeg_address', 48)
        self.assertEqual({k: app.store.get(imei)[k] for k in ('camera_address', 'camera_format')},
                         {'camera_address': 1, 'camera_format': 'Протокол2'})  # старые поля не трогаем
        with patch.object(app, 'navigate') as navigate:
            app.open_camera()
            navigate.assert_called_once_with('Камера номеров')
        # «Сейчас»: камера отвечает по статусу 2, пустая строка без отметки.
        self.fake.periphery = bytes([1]) + bytes(7)
        app.command(22)
        self.assertTrue(self.board.until(lambda: self.board.details and self.board.details['peripherals'].startswith('01'), 4))
        app.refresh_board()
        self.assertEqual([state.get() for _, _, state in app.peripherals[:2]], ['отвечает', '—'])
        self.assertIn('Камера: отвечает · адрес 48', app.plate_state['text'])
        self.assertIn('нет строки «9 — фотокамера JPEG»', app.jpeg_state['text'])
        # Захват 6 «485, адрес камеры»: номер латиницей с нулём из пакета 8.
        self.fake.reader_answer = b'A001XH197\x00'
        with patch.object(app, 'command', wraps=app.command) as command:
            app.read_plate()
            command.assert_called_once_with(6, bytes([15, 3, 48]))
        self.assertTrue(self.board.until(lambda: self.board.last_reader and self.board.last_reader[1] == 1, 4))
        app.refresh_board()
        self.assertEqual(app.plate_number['text'], 'A001XH197')
        self.assertEqual(app.plate_result['text'], 'Номер: A001XH197')
        with patch.object(app, 'edit_row') as edit:
            app.add_plate()
            edit.assert_called_once_with('ids', preset={'identifier': 'A001XH197', 'kind': 3})
    # Отдельный экземпляр окна; основной сервер 15010 не используется.
    def test_window_diagnostics_switching_and_refresh_recovery(self):
        from naviset_ui import App
        app = App(self.server, self.messages, self.server.say)
        app.root.withdraw()
        self.addCleanup(close_window, app)
        app.root.update()
        app.select()
        app.pump_once()
        self.assertEqual(app.imei, self.board.imei)
        with patch.object(app, 'command', return_value=None) as command:
            app.jpeg_address.set('2')
            app.take_jpeg()  # прошивка принимает только один байт адреса
            command.assert_called_once_with(7, bytes([2]))
        self.assertEqual(app.store.get(self.board.imei)['jpeg_address'], 2)
        app.dashboard.start()
        deadline = time.monotonic() + 4
        while app.dashboard.runners and time.monotonic() < deadline:
            app.root.update()
            time.sleep(.02)
        self.assertFalse(app.dashboard.runners)
        self.assertTrue(app.dashboard.reports[self.board.imei]['passed'])
        self.assertIn('FW 71', app.dashboard.cards['firmware'][0]['text'])
        app.imei = '111111111111111'
        app.load_data()
        app.refresh_board()
        self.assertEqual(app.dashboard.cards['firmware'][0]['text'], '—')
        self.assertEqual(app.jpeg_address.get(), '')  # без копии настроек адрес не подставляется молча
        self.assertEqual(app.plate_address.get(), '48')
        self.assertIn('Копия настроек не загружена', app.jpeg_note['text'])
        with self.assertRaisesRegex(ValueError, '^Введите адрес JPEG-камеры'):
            app.take_jpeg()  # пустое поле — подсказка по-русски, а не «invalid literal for int()»
        app.plate_address.set('abc')
        with self.assertRaisesRegex(ValueError, 'введите число 1–255'):
            app.shoot_plate()
        self.assertIn('этой платы', app.firmware_progress.get())
        with self.assertRaisesRegex(ValueError, 'не на связи'):
            app.command(8, bytes([3, 1, 0]))
        with patch.object(app, 'pump_once', side_effect=RuntimeError('temporary')), patch.object(app.root, 'after') as after:
            app.pump()
            after.assert_called_once_with(700, app.pump)
        self.assertIn('Повторяем', app.notice.get())
        app.imei = self.board.imei
        app.refresh_board()
        self.assertEqual(str(app.dashboard.report_button['state']), 'normal')
        app.dashboard.reports.clear()
        app.dashboard.displayed = None
        app.refresh_board()
        self.assertEqual(str(app.dashboard.report_button['state']), 'normal')
        self.assertIn('прошлого подключения', app.dashboard.summary.get())

    def test_preview_shows_selected_file(self):
        import tkinter as tk
        from PIL import Image
        from naviset_photo import PhotoPreview
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(lambda: (root.destroy(), gc.collect()))  # цикл preview↔canvas собираем здесь, не в потоке сервера
        root.update_idletasks()
        preview = PhotoPreview(root)
        preview.pack()
        Image.new('RGB', (640, 480), '#164c7e').save(self.folder / f'{self.board.imei}_1.jpg')
        preview.show(self.folder / f'{self.board.imei}_1.jpg')
        self.assertIsNotNone(preview.image)
        self.assertIn('640 × 480', preview.caption.get())
        preview.show(None)
        self.assertIsNone(preview.image)
        self.assertIsNone(preview.path)
        (self.folder / '111111111111111_2.jpg').write_bytes(b'not a jpeg')
        preview.show(self.folder / '111111111111111_2.jpg')
        self.assertIsNone(preview.image)
        self.assertIn('повреждён', preview.placeholder)

    def open_app(self):
        from naviset_ui import App
        app = App(self.server, self.messages, self.server.say)
        app.root.withdraw()
        self.addCleanup(close_window, app)
        app.root.update()
        app.select()
        return app

    def test_camera_tabs_result_lines_and_shot_lists(self):
        app = self.open_app()
        imei = self.board.imei

        def wait(label, text, timeout=8):
            deadline = time.monotonic() + timeout
            while True:
                app.refresh_board()
                app.root.update()
                if text in label['text'] or time.monotonic() > deadline:
                    return label['text']
                time.sleep(.05)
        app.refresh_board()
        self.assertEqual(app.jpeg_result['text'], 'С этой вкладки команд ещё не было')
        # Снимок JPEG-камеры — только на её вкладке; старый файл, упавший в папку во время съёмки, не засчитывается.
        self.fake.shot = jpeg((640, 480), jfif=False)
        app.jpeg_address.set('9')
        app.take_jpeg()
        stand.PHOTOS.mkdir(parents=True, exist_ok=True)
        (stand.PHOTOS / f'{imei}_1700000000.jpg').write_bytes(self.fake.shot)
        self.assertEqual(wait(app.jpeg_result, 'Снимок получен'), f'Снимок получен: {imei}_{self.fake.photo_number}.jpg')
        name = f'{imei}_{self.fake.photo_number}.jpg'
        self.assertIn(name, app.jpeg_shots.list.get_children())
        self.assertNotIn(name, app.plate_shots.list.get_children())
        self.assertEqual(app.jpeg_shots.list.set(name, 'cause'), 'снимок по команде с сервера')
        logged = []
        while not self.messages.empty():
            logged.append(self.messages.get()[1])
        self.assertTrue(any(f'Снимок №{self.fake.photo_number} принят: photos\\{name} (JPEG-камера)' in t for t in logged), logged)
        # Камеру не определить (заглушка имитатора) — снимок виден на обеих вкладках с пометкой, а не теряется.
        self.fake.shot = None
        app.shoot_plate()
        self.assertIn('Снимок получен', wait(app.plate_result, 'Снимок получен'))
        name = f'{imei}_{self.fake.photo_number}.jpg'
        self.assertIn(name, app.jpeg_shots.list.get_children())
        self.assertTrue(app.plate_shots.list.set(name, 'reason').startswith('камера? · '))
        self.assertIn('камера не определена: 1', app.plate_shots.count.get())
        # «Считать номер и снять»: пакет 8 с номером — сразу команда 7; кадр камеры номеров — только на её вкладке.
        self.fake.shot, self.fake.reader_answer = jpeg((384, 216)), b'A001XH197\x00'
        app.read_plate(True)
        self.assertIn('Снимок получен', wait(app.plate_result, 'Снимок получен'))
        self.assertTrue(app.plate_result['text'].startswith('Номер: A001XH197 · Снимок получен'), app.plate_result['text'])
        name = f'{imei}_{self.fake.photo_number}.jpg'
        self.assertIn(name, app.plate_shots.list.get_children())
        self.assertNotIn(name, app.jpeg_shots.list.get_children())
        self.assertEqual(app.plate_number['text'], 'A001XH197')
        # Отказ 2 на снимок камеры номеров — понятная причина, а не «ошибка выполнения».
        self.fake.behavior = 'refuse7'
        app.shoot_plate()
        self.assertIn('не видела номер последние 5 с', wait(app.plate_result, 'не видела номер'))
        self.assertEqual(str(app.plate_result['foreground']), '#b42318')

    def test_camera_result_waits_for_own_shot(self):
        from naviset_shots import shot_failed
        app = self.open_app()
        board = self.board
        now = time.time()
        base = dict(imei=board.imei, cmd=7, uid=0, address=9, shoot=False, prefix='', answer=0, mark=0, number=None,
                    final=None, camera='jpeg', files=set(), seen=10**9, started=now - 61, answered=now - 61)
        app.pending['JPEG-камера'] = dict(base)
        with board.lock:
            board.busy_photo = True  # 60 с прошло, но снимок ещё идёт — это не «не получился»
            self.assertEqual(app.camera_result('JPEG-камера', board), ('Снимок ещё передаётся, ждём…', ''))
            board.busy_photo = False
            self.assertEqual(app.camera_result('JPEG-камера', board), (shot_failed('jpeg'), 'bad'))
        # Событие 129 после команды — «камера сняла»; файл с этим номером от другой камеры — где его искать.
        number = int(now)
        with board.lock:
            board.journal.append(ShotTests.record(10**8, number, 129, struct.pack('<I', number)))
            app.pending['Камера номеров'] = dict(base, camera='plate', seen=10**8 - 1, started=now, answered=now)
            self.assertEqual(app.camera_result('Камера номеров', board)[0], 'Камера сняла, «Нависет» забирает снимок…')
            app.shots = [dict(name='чужой.jpg', number=number - 3600, kind='plate'), dict(name='x.jpg', number=number, kind='jpeg')]
            text, tone = app.camera_result('Камера номеров', board)
        self.assertEqual((text, tone), ('Снимок получен: x.jpg — снимала JPEG-камера, он на вкладке «JPEG-камера»', 'good'))


def jpeg(size, jfif=True):
    """Синтетический кадр: Pillow пишет APP0 JFIF, как камера номеров; без него — как JPEG-камера C328 (сразу FF DB)."""
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', size, '#164c7e').save(buffer, format='JPEG')
    data = buffer.getvalue()
    assert data[2:4] == b'\xff\xe0'
    return data if jfif else data[:2] + data[4 + int.from_bytes(data[4:6], 'big'):]


class ShotTests(unittest.TestCase):
    """Какая камера сняла, почему, и какой адрес подставить — без окна и платы."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='naviset-shots-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.bare, self.plate = jpeg((640, 480), jfif=False), jpeg((384, 216))

    @staticmethod
    def record(index, stamp, kind, arg=b''):
        return stand.Record(struct.pack('<IIB', index, stamp, kind) + arg.ljust(10, b'\0') + struct.pack('<IB', 0, 255) + bytes(8))

    def test_camera_kind_by_jpeg_header(self):
        from naviset_shots import camera_kind, jpeg_header, photo_info
        path = self.folder / 'shot.jpg'
        path.write_bytes(self.bare)
        self.assertEqual(self.bare[:4], b'\xff\xd8\xff\xdb')
        self.assertEqual(photo_info(path), ('jpeg', 640, 480))
        path.write_bytes(self.plate)  # другой размер: кэш не мешает
        self.assertEqual(photo_info(path), ('plate', 384, 216))
        self.assertEqual(camera_kind(path), 'plate')
        self.assertEqual(jpeg_header(jpeg((640, 480))), ('unknown', 640, 480))  # JFIF 640×480 — JPEG-модуль OV, не Interceptor
        self.assertEqual(jpeg_header(self.plate[:2] + b'\xff' + self.plate[2:]), ('plate', 384, 216))  # байт-заполнитель
        self.assertEqual(jpeg_header(self.bare[:2] + b'\xff\xe1\x00\x08Exif\0\0' + self.bare[2:])[0], 'unknown')
        for broken in (b'\xff\xd8\xff', self.bare[:20], b'\xff\xd8' + bytes(range(256)) * 9 + b'\xff\xd9'):
            self.assertEqual(jpeg_header(broken)[0], 'unknown')

    def test_shot_list_and_cause_from_journal(self):
        from naviset_shots import NO_CAUSE, list_shots
        imei = '123456789012345'
        for number, data in ((1793287525, self.plate), (1791987402, self.bare), (1792553538, self.bare), (1792560000, self.bare)):
            (self.folder / f'{imei}_{number}.jpg').write_bytes(data)
        (self.folder / '111111111111111_1793287600.jpg').write_bytes(self.plate)
        (self.folder / f'{imei}_1793287700.tmp').write_bytes(self.plate)
        for stray in ('20261029153625', '²³', 'abc', '1_2', '0123', '123'):  # посторонние имена не роняют список
            (self.folder / f'{imei}_{stray}.jpg').write_bytes(self.bare)
        journal = [self.record(1, 1793287524, 73, b'T222YY777'), self.record(2, 1791987402, 69, b'9635775629'),
                   self.record(3, 1792553538, 18), self.record(4, 1793287520, 69, b'9031234567')]
        shots = list_shots(self.folder, imei, journal, commands=[1792559999.4])
        self.assertEqual([s['number'] for s in shots], [1793287525, 1792560000, 1792553538, 1791987402, 123, 123])
        self.assertEqual({s['name'] for s in shots[4:]}, {f'{imei}_0123.jpg', f'{imei}_123.jpg'})
        plate, command, unknown, phone = shots[:4]
        local = time.strftime('%d.%m %H:%M:%S', time.localtime(1793287525))  # 29.10 15:25:25 UTC — по часам этого компьютера
        self.assertEqual((plate['kind'], plate['cause'], plate['plate'], plate['reason'], plate['time']),
                         ('plate', 'проход по номеру T222YY777', 'T222YY777', 'проход по номеру', local))
        self.assertEqual((phone['kind'], phone['cause'], phone['ident'], phone['plate'], phone['reason']),
                         ('jpeg', 'проход по телефону 9635775629', '9635775629', '', 'проход по телефону 9635775629'))
        self.assertEqual(unknown['cause'], NO_CAUSE)  # «потеря связи» — не причина снимка
        self.assertEqual(command['cause'], 'снимок по команде с сервера')

    def test_cause_window_priority_and_events_without_shot(self):
        from naviset_shots import NO_CAUSE, shot_cause
        n, r = 1793287600, self.record
        self.assertEqual(shot_cause(n, [r(1, n - 2, 69, b'9031234567')])[0], 'проход по телефону')
        for stamp in (n - 3, n + 1):
            self.assertEqual(shot_cause(n, [r(1, stamp, 69, b'9031234567')])[0], NO_CAUSE)
        # 65 и 66 выходы не открывают — снимка у них нет; проход по API рядом — причина.
        self.assertEqual(shot_cause(n, [r(1, n - 1, 65, b'X999XX77'), r(2, n, 75, b'api')], kind='plate')[:2], ('проход по API', 'api'))
        self.assertEqual(shot_cause(n, [r(1, n, 66, b'X999XX77')])[0], NO_CAUSE)
        # Камере номеров номер важнее телефона в ту же секунду; JPEG-камере — ближайшее событие.
        both = [r(1, n - 1, 73, b'T222YY777'), r(2, n, 69, b'9031234567')]
        self.assertEqual(shot_cause(n, both, kind='plate')[0], 'проход по номеру')
        self.assertEqual(shot_cause(n, both, kind='jpeg')[0], 'проход по телефону')
        # «Считать номер и снять»: 78 опережает снимок на опрос и очередь — ищется до 15 с, но не у JPEG-камеры.
        read = [r(1, n - 9, 78, b'T222YY777')]
        self.assertEqual(shot_cause(n, read, kind='plate'), ('по команде (номер считан)', 'T222YY777', 78))
        self.assertEqual(shot_cause(n, read, kind='jpeg')[0], NO_CAUSE)
        self.assertEqual(shot_cause(n, [r(1, n - 16, 78, b'T222YY777')], kind='plate')[0], NO_CAUSE)

    def test_camera_address_from_settings_copy(self):
        from naviset_shots import JPEG_TYPE, PLATE_DEFAULT, PLATE_TYPE, camera_address, camera_of_address
        raw = bytearray(512)
        raw[179:183] = bytes([48, PLATE_TYPE, 7, JPEG_TYPE])
        self.assertEqual(camera_address(raw, JPEG_TYPE, 3)[0], '7')
        self.assertEqual(camera_address(raw, PLATE_TYPE, 5)[0], '48')
        self.assertEqual([camera_of_address(raw, a) for a in (48, 7, 5)], ['plate', 'jpeg', None])
        value, note = camera_address(b'', JPEG_TYPE)
        self.assertEqual(value, '')
        self.assertTrue(note.startswith('Копия настроек не загружена'))
        value, note = camera_address(bytes(512), JPEG_TYPE, 5)
        self.assertEqual(value, '5')
        self.assertTrue(note.startswith('В таблице 485 нет строки «9 — фотокамера JPEG»'))
        self.assertEqual(camera_address(b'', PLATE_TYPE), ('48', PLATE_DEFAULT))
        self.assertEqual(camera_address(b'', PLATE_TYPE, 50)[0], '50')

    def test_result_texts_by_code(self):
        from naviset_shots import answer_text, reader_text, shot_done, shot_failed
        self.assertEqual(answer_text(7, 0, 'jpeg', 9), 'Съёмка начата, ждём снимок…')
        self.assertEqual(answer_text(7, 1, 'jpeg', 9), 'Плата: камеры с адресом 9 нет в таблице 485, '
                                                       'или «Режим RS-485» не «Активный опрос», или включён Wi-Fi-модем')
        self.assertEqual(answer_text(6, 1, 'plate', 48), 'Плата отвергла параметры захвата (1)')
        self.assertTrue(answer_text(7, 2, 'plate', 48).startswith('Камера номеров не видела номер последние 5 с'))
        self.assertIn('не установлены часы', answer_text(7, 2, 'jpeg', 9))
        self.assertEqual(answer_text(6, 0, 'plate', 48), 'Камера ищет номер (до 15 с)…')
        self.assertEqual(answer_text(7, 255, 'jpeg', 9), 'Плата: не поддерживается (255)')
        self.assertEqual(reader_text(1, b'A001XH197\0'), 'Номер: A001XH197')
        self.assertIn('камера с адресом 48 не опрашивается', reader_text(255, bytes(10), 48))
        self.assertIn('пока видит номер', shot_failed('plate'))
        self.assertNotIn('номер', shot_failed('jpeg'))
        shot = dict(name='a_1.jpg', kind='plate')
        self.assertEqual(shot_done(shot, 'plate'), 'Снимок получен: a_1.jpg')
        self.assertIn('на вкладке «Камера номеров»', shot_done(shot, 'jpeg'))

    def test_last_plate_and_camera_state_without_settings_copy(self):
        from naviset_shots import JPEG_TYPE, PLATE_TYPE, camera_state, latest_plate
        journal = [self.record(1, 1793288000, 73, b'T222YY777'), self.record(2, 1793288090, 65, b'9031234567'),
                   self.record(3, 1793288091, 65, b'T222XY777')]
        self.assertEqual(latest_plate(None, journal), (1793288091, 'T222XY777', 'номера нет в базе'))
        self.assertEqual(latest_plate((1793288092.5, 1, b'A001XH197\0'), journal)[1:], ('A001XH197', 'считан по команде'))
        self.assertIsNone(latest_plate((1793288092.5, 255, bytes(10)), journal[1:2]))  # телефон — не номер
        details = dict(time=1793288100, peripherals='00 01 00 00 00 00 00 00')
        text = camera_state(details, b'', PLATE_TYPE, journal)
        self.assertIn('без копии настроек не видно', text)
        self.assertIn('отвечают строки 485: 2', text)
        self.assertIn('номер от камеры 29.10', text)
        self.assertIn('Обновить (статус 2)', camera_state(None, b'', JPEG_TYPE))
        raw = bytearray(512)
        raw[181:183] = bytes([7, JPEG_TYPE])
        self.assertTrue(camera_state(details, raw, JPEG_TYPE).startswith('Камера: отвечает · адрес 7 · строка 2'))


if __name__ == '__main__':
    unittest.main()
