"""Регрессии Нависет: локальная модель, отдельная БД, без реального оборудования."""
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

    def command(self, uid, cmd, data):
        self.received.append(cmd)
        if cmd == 22 and self.behavior == 'reject':
            self.answer(cmd, uid, 2)
        elif cmd == 22 and self.behavior == 'silent':
            return
        else:
            super().command(uid, cmd, data)


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


class WindowTests(unittest.TestCase):
    setUp = NetworkTests.setUp
    cleanup_server = NetworkTests.cleanup_server

    def test_rs485_table_status_and_number_camera(self):
        from naviset_ui import App, periphery_choice
        app = App(self.server, self.messages, self.server.say)
        app.root.withdraw()

        def close():  # без вопроса окна и без отложенного обновления в следующем тесте
            app._closing = True
            for pending in app.root.tk.call('after', 'info'):
                app.root.after_cancel(pending)
            app.root.destroy()
        self.addCleanup(close)
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
        app.save_settings()
        self.assertTrue(self.board.until(lambda: self.fake.tables[12][172] == 1 and self.fake.tables[12][179:183] == bytes([48, 8, 0, 0]), 4))
        # Адрес камеры для снимка и захвата берётся из таблицы 485, даже если раньше сохранили другой.
        data = app.store.get(imei)
        data['camera_address'] = 5
        app.store.put(imei, data)
        app.load_data()
        self.assertEqual(app.camera.get(), '48')
        # «Сейчас»: камера отвечает по статусу 2, пустая строка без отметки.
        self.fake.periphery = bytes([1]) + bytes(7)
        app.command(22)
        self.assertTrue(self.board.until(lambda: self.board.details and self.board.details['peripherals'].startswith('01'), 4))
        app.refresh_board()
        self.assertEqual([state.get() for _, _, state in app.peripherals[:2]], ['отвечает', '—'])
        self.assertIn('камера номеров, адрес 48: отвечает', app.plate_note.get())
        # Захват 6 «485, адрес камеры»: номер латиницей с нулём из пакета 8.
        self.fake.reader_answer = b'A001XH197\x00'
        with patch.object(app, 'command', wraps=app.command) as command:
            app.read_plate()
            command.assert_called_once_with(6, bytes([15, 3, 48]))
        self.assertTrue(self.board.until(lambda: self.board.last_reader and self.board.last_reader[1] == 1, 4))
        app.refresh_board()
        self.assertIn('Считан номер «A001XH197»', app.plate_note.get())
        with patch.object(app, 'edit_row') as edit:
            app.add_plate()
            edit.assert_called_once_with('ids', preset={'identifier': 'A001XH197', 'kind': 3})
    # Отдельный экземпляр окна; основной сервер 15010 не используется.
    def test_window_diagnostics_switching_and_refresh_recovery(self):
        from naviset_ui import App
        app = App(self.server, self.messages, self.server.say)
        app.root.withdraw()
        self.addCleanup(app.root.destroy)
        app.root.update()
        app.select()
        app.pump_once()
        self.assertEqual(app.imei, self.board.imei)
        with patch.object(app, 'command') as command:
            app.camera.set('2')
            app.take_photo()
            command.assert_called_once_with(7, bytes([2]))
            command.reset_mock()
            app.camera_format.set('Протокол2')
            app.take_photo()
            command.assert_called_once_with(7, bytes([7, 2]))
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
        self.assertEqual(app.camera_format.get(), 'ДОМОВОЙ NEXT')
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

    def test_received_photo_shows_and_switching_clears_it(self):
        import tkinter as tk
        from PIL import Image
        from naviset_photo import PhotoPreview
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        root.update_idletasks()
        preview = PhotoPreview(root)
        preview.pack()
        self.folder.mkdir(exist_ok=True)
        image = Image.new('RGB', (640, 480), '#164c7e')
        image.save(self.folder / f'{self.board.imei}_1.jpg')
        preview.refresh(self.board.imei, self.folder)
        self.assertIsNotNone(preview.image)
        self.assertIn('640 × 480', preview.caption.get())
        preview.refresh('111111111111111', self.folder)
        self.assertIsNone(preview.image)
        self.assertIsNone(preview.path)
        (self.folder / '111111111111111_2.jpg').write_bytes(b'not a jpeg')
        preview.refresh('111111111111111', self.folder)
        self.assertIsNone(preview.image)
        self.assertIn('повреждён', preview.placeholder)


if __name__ == '__main__':
    unittest.main()
