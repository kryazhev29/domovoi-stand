# Доработка: Кряжев А., 2026
import struct
import unittest
from pathlib import Path
from rs485_config import prepare_config, crc16, load_names, differences, read_config, DEFAULT_FILE
from photo_server_gui import build, take_frames, Server
from types import SimpleNamespace

class ConfigTest(unittest.TestCase):
    def test_selected_peer_and_answers(self):
        server = Server()
        peer = SimpleNamespace(imei='123456789012345', status=0, transfer=None)
        server.peers['chosen'] = peer
        sent = []
        server.send = lambda target, command, payload: sent.append((target, command, payload))
        server.command('chosen', 'settings485', {'config': bytes(512)})
        server.do_actions()
        self.assertEqual(sent, [(peer, 12, bytes(512))])
        server.handle(peer, 1, 11, bytes([12, 2]))
        server.handle(peer, 1, 12, bytes([4])+bytes(6))
        events = []
        while not server.out.empty(): events.append(server.out.get())
        answers = [value for kind,value in events if kind == 'settings_result']
        self.assertTrue(any('ответ 11' in value and 'код 2' in value for value in answers))
        self.assertTrue(any('приняты' in value for value in answers))

    def test_command12_only_selected_bytes(self):
        raw = bytearray((i*37+11) & 255 for i in range(512))
        raw[8:40] = b'192.0.2.1:15010'.ljust(32, b'\0')
        raw[219] = 1
        values = {172: 1, 174: 4, 178: 2, **{i: (i*7) & 255 for i in range(179, 195)}}
        payload = prepare_config(bytes(raw), values, '192.0.2.1:15010')
        allowed = set(values) | {510, 511}
        self.assertTrue(all(a == b for i,(a,b) in enumerate(zip(raw,payload)) if i not in allowed))
        self.assertEqual(payload[175:178], raw[175:178])
        self.assertEqual(crc16(payload[:510]), int.from_bytes(payload[510:], 'little'))
        packet = build(12, payload, 43)
        frames, rest = take_frames(bytearray(packet))
        self.assertEqual(frames, [(43, 12, payload)])
        self.assertFalse(rest)
        self.assertEqual(packet[183], 4)
        self.assertEqual(struct.unpack_from('<H', packet, 4)[0], 515)

    def test_endpoint_and_unknown_values(self):
        raw = bytearray(512); raw[219] = 1
        payload = prepare_config(raw, {180:255, 179:0, 174:255}, '192.0.2.2:15010')
        self.assertEqual(payload[180], 255)
        self.assertEqual(payload[174], 255)
        self.assertEqual(payload[8:40].split(b'\0')[0], b'192.0.2.2:15010')
        with self.assertRaises(ValueError): prepare_config(raw, {175:1}, '192.0.2.2:15010')
        with self.assertRaises(ValueError): prepare_config(raw, {}, '0.0.0.0:15010')
        names = load_names(Path(__file__).with_name('periphery.ini'))
        self.assertIn('255', '\n'.join(differences(raw, payload, names)))

    @unittest.skipUnless(DEFAULT_FILE.exists(), 'USB-файл доступен только на стенде')
    def test_actual_usb_format(self):
        self.assertEqual(len(read_config(DEFAULT_FILE)),512)

if __name__ == '__main__': unittest.main()
