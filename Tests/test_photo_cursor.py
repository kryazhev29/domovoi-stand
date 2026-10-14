"""Курсор фото: локальные файлы и модель сервера, без сети."""
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import naviset_server as server


class PhotoCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        photos = patch.object(server.stand, 'PHOTOS', self.root / 'received')
        photos.start(); self.addCleanup(photos.stop)
        self.board = server.Board(lambda *args: None, state_dir=self.root)
        self.board.imei = '867442070217472'
        self.board.send = Mock()
        self.board.busy_photo = True
        self.cursor = self.root / 'photos' / self.board.imei / 'cursor.json'

    def data(self, offset, chunk):
        self.board.photo_packet(b'\1' + struct.pack('<IIH', 12345, 4, offset) + chunk)

    def test_completed_cursor_clears_only_after_ack_empty(self):
        self.data(0, b'abcd')
        self.assertEqual(json.loads(self.cursor.read_text()), [12345, 4])
        self.board.photo_packet(b'\0')
        self.board.send.assert_called_once_with(21, struct.pack('<IH', 12345, 4))
        self.assertTrue(self.cursor.exists())
        self.assertTrue(self.board.busy_photo)
        self.board.photo_packet(b'\0')
        self.assertEqual(self.board.photo_cursor, (0, 0))
        self.assertIsNone(self.board.photo_last_complete)
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.board.busy_photo)
        # Следующий статус запрашивает новое фото с нулевого курсора.
        self.board.send.reset_mock()
        self.board.queue = Mock(); self.board.queue.rows.return_value = []
        self.board.reg = {}; self.board.auto_photo = True
        self.board.wire = Mock()
        self.board.handle(4, 2, struct.pack('<IHH', int(time.time()), 0x2000, 2048))
        self.board.send.assert_called_once_with(21, bytes(6))
        # Повторное подключение после подтверждения тоже не загрузит старый курсор.
        fresh = server.Board(lambda *args: None, state_dir=self.root)
        fresh.imei = self.board.imei; fresh.sync_structures()
        self.assertEqual(fresh.photo_cursor, (0, 0))

    def test_partial_and_unconfirmed_survive_reconnect(self):
        for chunk in (b'ab', b'abcd'):
            with self.subTest(chunk=chunk):
                self.data(0, chunk)
                fresh = server.Board(lambda *args: None, state_dir=self.root)
                fresh.imei = self.board.imei; fresh.sync_structures()
                self.assertEqual(fresh.photo_cursor, (12345, len(chunk)))
        self.assertTrue(self.cursor.exists())

    def test_partial_empty_requests_missing_bytes(self):
        self.data(0, b'ab')
        self.board.photo_packet(b'\0')
        self.board.send.assert_called_once_with(21, struct.pack('<IH', 12345, 2))
        self.assertTrue(self.cursor.exists())
        self.assertTrue(self.board.busy_photo)


if __name__ == '__main__':
    unittest.main()
