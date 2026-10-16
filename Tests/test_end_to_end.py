"""Запускает настоящие test_server.py --headless и fake_board.py на localhost."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EndToEnd(unittest.TestCase):
    def run_case(self, checks, fault='', expected=True, reason='', timeout=160):
        with tempfile.TemporaryDirectory(prefix='domovoi-stand-') as d:
            folder = Path(d)
            image = folder/'139.71.1791001234.bin'
            image.write_bytes(bytes(range(256))*12)
            config = folder/'config.bin'
            config.write_bytes(bytes(512))
            # Каждый процесс получает отдельный временный порт и каталог результатов.
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            result = folder/'result.json'
            command = [sys.executable, '-B', '-X', 'utf8', str(ROOT/'test_server.py'),
                       '--headless', '--bind', '127.0.0.1', '--port', str(port),
                       '--run', *checks, '--firmware', str(image), '--phone', '9990000000',
                       '--config', str(config),
                       '--wait-board', '10', '--log-dir', d, '--json', str(result),
                       '--update-timeout', '3']
            with (folder/'server.log').open('w', encoding='utf-8') as log:
                server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                board = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(ROOT/'fake_board.py'),
                                          '--port', str(port), '--fault', fault],
                                         stdout=log, stderr=subprocess.STDOUT)
                try:
                    code = server.wait(timeout=timeout)
                finally:
                    for process in (server, board):
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=5)
            output = (folder/'server.log').read_text(encoding='utf-8')
            self.assertTrue(result.exists(), output[-4000:])
            rows = json.loads(result.read_text(encoding='utf-8'))
            self.assertEqual(code == 0, expected, str(rows) + '\n' + output[-3500:])
            self.assertEqual(all(row[1] for row in rows), expected, str(rows))
            if reason:
                self.assertIn(reason, str(rows))
            print(f'PASS {fault or "normal"}: ' + ', '.join(f'{n}={ok}' for n, ok, _ in rows), flush=True)
            return rows

    def test_full_protocol(self):
        rows = self.run_case(['full'])
        self.assertEqual(len(rows), 18)

    def test_outputs(self):
        self.run_case(['выходы'])

    def test_reconnect_without_close(self):
        self.run_case(['перезагрузка'], 'reconnect-open')

    def test_lost_update_answer(self):
        self.run_case(['обновление', 'журнал'], 'lost11')

    def test_failures(self):
        for checks, fault, reason in (
            (['crc'], 'hang', 'нет пакета 5'),
            (['неизвестная'], 'unknown99', 'нужно 255'),
            (['праздники'], 'holiday-tail', 'список из 1 дат'),
            (['обновление'], 'no-progress', 'нет завершения загрузки'),
            (['обновление'], 'wrong-build', 'установлен другой образ'),
            (['обновление'], 'missing-event', 'нет событий [136]'),
        ):
            with self.subTest(fault=fault):
                self.run_case(checks, fault, False, reason, timeout=35)


if __name__ == '__main__':
    unittest.main()
