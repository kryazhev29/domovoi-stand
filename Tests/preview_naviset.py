"""Изолированное окно для ручной проверки интерфейса. Закрытие удаляет тестовые данные."""
import io
import queue
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from fake_board import FakeBoard
import test_server as stand
from naviset_server import Server
from naviset_ui import App


def main():
    with tempfile.TemporaryDirectory(prefix='naviset-preview-') as temporary:
        stand.LOGS = Path(temporary)
        stand.PHOTOS = stand.LOGS / 'photos'
        messages = queue.Queue()
        say = lambda text, kind='info': messages.put((kind, text))
        server = Server(0, say, '127.0.0.1', state_dir=stand.LOGS / 'state')
        server.start()
        server.ready.wait(3)
        fake = FakeBoard(port=server.port)
        picture = Image.new('RGB', (640, 480), '#173451')
        draw = ImageDraw.Draw(picture)
        for x in range(0, 640, 40):
            draw.line((x, 0, x, 480), fill='#244766')
        for y in range(0, 480, 40):
            draw.line((0, y, 640, y), fill='#244766')
        draw.rectangle((120, 110, 520, 370), outline='#48bfb5', width=4)
        draw.text((255, 215), 'CAMERA PREVIEW', fill='white')
        draw.text((272, 245), '640 x 480', fill='#48bfb5')
        buffer = io.BytesIO()
        picture.save(buffer, format='JPEG')
        fake.photo = buffer.getvalue()
        fake.photo_number = 1791000000
        worker = threading.Thread(target=fake.run, daemon=True)
        worker.start()
        server.board.until(lambda: server.board.reg and server.board.status, 3)
        data = server.data_store.get('123456789012345')
        data.update(name='Тестовая плата', object='Локальная модель · без оборудования')
        server.data_store.put('123456789012345', data)
        app = App(server, messages, say)
        app.root.title('ДОМОВОЙ · проверка интерфейса (модель)')
        try:
            app.run()
        finally:
            fake.stop.set()
            worker.join(2)
            server.stop()
            for board in server.boards.values():
                if board.queue:
                    board.queue.db.close()


if __name__ == '__main__':
    main()
