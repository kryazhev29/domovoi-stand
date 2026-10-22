# Стенд ДОМОВОЙ NEXT — скрипты

Инструменты проверки контроллера ДОМОВОЙ NEXT (плата v5 HW139, STM32F103, модем SLM336) на столе, Windows.
Прошивка — https://github.com/kryazhev29/domovoi-next, сервисное ПО — https://github.com/kryazhev29/nextconfig.

COM5 — плата по USB, COM14 — свисток USB-RS485.

- Заливка по USB: `try_fw.sh`, `fw_usb_update.py`, `fw_usb_rollback.py`, загрузчик `boot_*.py`.
- Настройки и состояние: `read_config.py`, `set_server1.py`, `next_status.py`, `usb_reboot.py`, `link_diag.py`.
- USB-журнал платы: `usb_watch.py`, `next_log.py`, `next_journal.py`.
- Wi-Fi-мост DR164: `wifi_setup.py`, опыты `dr164_lab.py`, `dr164_timing.py`, `dr164_entm.py`.
- Камера: `usb_photo.py`, `cam_probe.py`, `camera_emulator.py`, `photo_*.py`.
- Тестовый сервер «Нависет» по Протоколу2: `naviset_*.py`, `Запуск_сервера_Нависет.pyw`.
- Пассивная запись линии 485: `line_sniff.py`.

Подробнее — `КАК_ПОЛЬЗОВАТЬСЯ.txt`, `Нависет_как_пользоваться.txt`; тесты — `Tests/`.
