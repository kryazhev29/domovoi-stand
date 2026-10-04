"""Проверки самого стенда без платы, COM и внешней сети."""
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_server as srv


class StandTests(unittest.TestCase):
    def test_new_session_survives_old_close(self):
        b = srv.Board(lambda *a: None)
        a, aa = socket.socketpair()
        c, cc = socket.socketpair()
        threads = []
        def serve(s):
            try:
                b.serve(s)
            except OSError:
                pass
            finally:
                s.close()
        try:
            for sock in (a, c):
                threads.append(threading.Thread(target=serve, args=(sock,), daemon=True))
                threads[-1].start()
                limit = time.monotonic() + 2
                while b.conn is not sock and time.monotonic() < limit:
                    time.sleep(.01)
            aa.close()
            threads[0].join(1)
            self.assertIs(b.conn, c)
        finally:
            aa.close(); cc.close()
            for t in threads:
                t.join(1)

    def test_update_is_last_before_journal(self):
        b = srv.Board(lambda *a: None)
        b.conn = object()
        c = srv.Checks(b, lambda *a: None, {})
        order = []
        for n in ('обновление', 'время', 'журнал'):
            setattr(c, 'c_' + n, lambda n=n: (order.append(n) is None, 'ok'))
        c.report = lambda results: None
        c.run(['обновление', 'журнал', 'время'])
        self.assertEqual(order, ['время', 'обновление', 'журнал'])

    def test_trigger_tail_preserved(self):
        b = srv.Board(lambda *a: None)
        b.status = (0, 0, 2048, time.monotonic())
        b.last_triggers = bytes([0, 0, 0, 0, 1, 1, 0, 1]) + bytes(8)
        sent = []
        b.ask = lambda cmd, p=b'', *a, **kw: (sent.append(p) or 0, (1, 11, bytes([cmd, 0])))
        b.wait = lambda *a, **kw: (1, 2, bytes(8))
        srv.Checks(b, lambda *a: None, {}).c_триггеры()
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(p[4:] == b.last_triggers[4:] for p in sent))

    def test_journal_diagnoses(self):
        b = srv.Board(lambda *a: None)
        self.assertFalse(b.pull_journal(.01))
        self.assertIn('нет связи', b.journal_error)
        b.conn = object()
        b.send = lambda *a: 1
        self.assertFalse(b.pull_journal(.01))
        self.assertIn('не отвечает', b.journal_error)
        def stream(*a):
            b.inbox.append((time.perf_counter(), 1, 10, bytes([0, 0, 1])+bytes(32)))
        b.send = stream
        self.assertFalse(b.pull_journal(.01))
        self.assertIn('выгрузка не закончилась', b.journal_error)

    def test_output_failure_reason(self):
        for flags, reason in ((0, 'не разрешено'), (2, 'выход занят')):
            b = srv.Board(lambda *a: None)
            b.status = (0, flags, 2048, time.perf_counter())
            b.ask = lambda cmd, p=b'', *a, **kw: (2, (1, 11, bytes([cmd, 2])))
            c = srv.Checks(b, lambda *a: None, {})
            c.output_off = lambda **kw: True
            ok, text = c.c_выходы()
            self.assertFalse(ok)
            self.assertIn(reason, text)

    def test_partial_photo_is_not_complete(self):
        with tempfile.TemporaryDirectory() as d:
            old = srv.PHOTOS
            srv.PHOTOS = Path(d)
            try:
                b = srv.Board(lambda *a: None)
                b.send = lambda *a: 1
                b.photo_packet(bytes([1])+srv.struct.pack('<IIH', 1, 4, 0)+b'ab')
                self.assertFalse(b.completed_photos)
                b.photo_packet(bytes([1])+srv.struct.pack('<IIH', 1, 4, 2)+b'cd')
                self.assertEqual(b.completed_photos, {1})
            finally:
                srv.PHOTOS = old


if __name__ == '__main__':
    unittest.main()
