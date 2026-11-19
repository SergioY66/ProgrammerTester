"""Обмен с Nano (прошивка firmware/), 250000 бод.

Запрос: A5 cmd len payload xor;  ответ: 5A status len payload xor.
"""

import time

import serial
import serial.tools.list_ports

BAUD = 250000

CMD_PING = 0x01
CMD_UPDI_OPEN = 0x10
CMD_UPDI_ERASE = 0x11
CMD_UPDI_ENTER = 0x12
CMD_UPDI_READ = 0x13
CMD_UPDI_FLASH = 0x14
CMD_UPDI_EEPROM = 0x15
CMD_UPDI_FUSE = 0x16
CMD_UPDI_STATUS = 0x17
CMD_UPDI_CLOSE = 0x18
CMD_CARD_TEST = 0x20
CMD_CHIP_TEST = 0x30
CMD_ISP_OPEN = 0x40
CMD_ISP_READ = 0x41
CMD_ISP_WRITE = 0x42
CMD_ISP_ERASE = 0x43
CMD_ISP_CLOSE = 0x44
CMD_ISP_READ_BLOCK = 0x45
CMD_ISP_WRITE_PART = 0x46
CMD_ISP_LOCK = 0x47
CMD_ISP_WRITE_BYTE = 0x48
CMD_SNIFF = 0x50

# Области памяти AT89 (firmware/include/isp89.h)
ISP_CODE, ISP_DATA, ISP_ATMEL, ISP_USER, ISP_LOCK, ISP_FUSES = range(6)

# Типы чипов для isp_open(force=…) — у закрытого от чтения сигнатура не читается
ISP_DEV_8253, ISP_DEV_S52 = 1, 2

ST_OK = 0
ST_UPDI_LOCKED = 15
ST_OW_DIFF = 32
ST_ISP_NOSIG = 42

STATUS_TEXT = {
    1: "ошибка пакета",
    2: "неизвестная команда",
    3: "неверные параметры",
    10: "изделие не отвечает по UPDI",
    11: "UPDI: нет подтверждения (ACK)",
    12: "UPDI: искажённый байт (чётность/стоп-бит)",
    13: "нет связи по UPDI — проверьте контакт, питание, UPDI",
    14: "UPDI: ключ не принят",
    15: "чип заблокирован (lock)",
    16: "не удалось войти в режим программирования",
    17: "NVM: превышено время записи",
    18: "NVM: ошибка записи",
    20: "карта не завершила обмен",
    30: "чип не ответил на сброс (нет импульса присутствия)",
    31: "линия 1-Wire прижата к нулю",
    32: "два чтения чипа не совпали",
    40: "AT89 не отвечает по ISP — проверьте прищепку, питание и кварц",
    41: "ISP: режим программирования не открыт",
    42: "AT89: сигнатура не опознана — чип закрыт от чтения или не в режиме программирования",
}


class DeviceError(Exception):
    def __init__(self, status, what=""):
        self.status = status
        text = STATUS_TEXT.get(status, f"код {status}")
        super().__init__(f"{what}: {text}" if what else text)


class IspSignatureUnknown(Exception):
    """Сигнатура не опознана: (прочитанная сигнатура, байт lock, ответ на Enable).
    Тип выбирает оператор."""

    def __init__(self, sig: bytes, lock: int, enable: int = 0xFF):
        self.sig = sig
        self.lock = lock
        self.enable = enable
        super().__init__(f"сигнатура {sig.hex(' ').upper()} не опознана")


def list_ports():
    return [(p.device, p.description) for p in serial.tools.list_ports.comports()]


class Device:
    def __init__(self, port: str):
        self.ser = serial.Serial(port, BAUD, timeout=0.2)
        time.sleep(2.0)                       # Nano перезапускается при открытии порта
        self.ser.reset_input_buffer()
        self.version = self.ping()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # --- транспорт ---

    def _send(self, cmd: int, payload: bytes = b""):
        if len(payload) > 255:
            raise ValueError("payload > 255")
        x = cmd ^ len(payload)
        for b in payload:
            x ^= b
        self.ser.reset_input_buffer()
        self.ser.write(bytes([0xA5, cmd, len(payload)]) + bytes(payload) + bytes([x]))

    def _transact(self, cmd: int, payload: bytes = b"", timeout: float = 3.0):
        self._send(cmd, payload)
        return self._receive(cmd, timeout)

    def _receive(self, cmd: int, timeout: float = 3.0):
        deadline = time.monotonic() + timeout

        def read(n):
            buf = b""
            while len(buf) < n:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Nano не ответила на команду 0x{cmd:02X}")
                buf += self.ser.read(n - len(buf))
            return buf

        while read(1) != b"\x5A":
            pass
        status, n = read(2)
        data = read(n)
        chk = read(1)[0]
        x = status ^ n
        for b in data:
            x ^= b
        if x != chk:
            raise IOError("Искажён ответ Nano (контрольная сумма)")
        return status, data

    def _cmd(self, cmd, payload=b"", timeout=3.0, what=""):
        status, data = self._transact(cmd, payload, timeout)
        if status != ST_OK:
            raise DeviceError(status, what)
        return data

    # --- команды ---

    def ping(self) -> int:
        d = self._cmd(CMD_PING, what="Nano")
        if d[:2] != b"PT":
            raise IOError("На порту не программатор-тестер")
        return d[2]

    def updi_open(self) -> int:
        return self._cmd(CMD_UPDI_OPEN, what="Подключение UPDI")[0]

    def updi_close(self):
        self._cmd(CMD_UPDI_CLOSE)

    def updi_erase_enter(self):
        self._cmd(CMD_UPDI_ERASE, timeout=5.0, what="Стирание")

    def updi_enter(self) -> bool:
        """True — в режиме программирования, False — чип заблокирован."""
        status, _ = self._transact(CMD_UPDI_ENTER)
        if status == ST_UPDI_LOCKED:
            return False
        if status != ST_OK:
            raise DeviceError(status, "Вход в программирование")
        return True

    def updi_status(self) -> int:
        return self._cmd(CMD_UPDI_STATUS)[0]

    def read(self, addr: int, n: int) -> bytes:
        out = b""
        while n:
            k = min(n, 128)
            out += self._cmd(CMD_UPDI_READ, bytes([addr & 0xFF, addr >> 8, k]), what=f"Чтение 0x{addr:04X}")
            addr += k
            n -= k
        return out

    def write_flash_page(self, offset: int, data: bytes):
        self._cmd(CMD_UPDI_FLASH, bytes([offset & 0xFF, offset >> 8]) + data,
                  what=f"Запись Flash 0x{offset:04X}")

    def write_eeprom(self, offset: int, data: bytes):
        self._cmd(CMD_UPDI_EEPROM, bytes([offset & 0xFF, offset >> 8]) + data,
                  what=f"Запись EEPROM 0x{offset:04X}")

    def write_fuse(self, addr: int, value: int):
        self._cmd(CMD_UPDI_FUSE, bytes([addr & 0xFF, addr >> 8, value]),
                  what=f"Запись фьюза 0x{addr:04X}")

    def card_test(self) -> bytes:
        return self._cmd(CMD_CARD_TEST, timeout=5.0, what="Тест карты")

    def chip_test(self, charge_ms: int):
        """(status, rom, mem). status — ST_OK или ST_OW_DIFF; иначе исключение."""
        status, data = self._transact(CMD_CHIP_TEST, bytes([charge_ms & 0xFF, charge_ms >> 8]),
                                      timeout=charge_ms / 1000 + 5.0)
        if status not in (ST_OK, ST_OW_DIFF):
            raise DeviceError(status, "Тест чипа")
        return status, data[:8], data[8:]

    # --- ISP AT89 (8253 / S52) ---

    def isp_open(self, force: int = 0):
        """(режим SPI, сигнатура[3], тип, байт lock, ответ на Programming Enable).

        force — ISP_DEV_8253/ISP_DEV_S52, если сигнатура не читается (чип закрыт).
        Исключение IspSignatureUnknown — сигнатуру опознать не удалось.
        Ответ на Enable есть только у прошивки Nano v7+; на старой — None.
        """
        status, data = self._transact(CMD_ISP_OPEN, bytes([force]), timeout=5.0)
        if status == ST_ISP_NOSIG:
            raise IspSignatureUnknown(bytes(data[1:4]), data[5], self._enable(data))
        if status != ST_OK:
            raise DeviceError(status, "Подключение ISP")
        return (data[0], bytes(data[1:4]), data[4], data[5], self._enable(data))

    def _enable(self, data: bytes):
        """Ответ на Programming Enable; None — прошивка Nano его не присылает (до v7)."""
        return data[6] if self.version >= 7 and len(data) > 6 else None

    def isp_close(self):
        self._cmd(CMD_ISP_CLOSE)

    def isp_read(self, space: int, addr: int, n: int) -> bytes:
        return self._cmd(CMD_ISP_READ, bytes([space, addr & 0xFF, addr >> 8, n]),
                         what=f"Чтение ISP 0x{addr:04X}")

    def isp_read_block(self, space: int, addr: int, n: int, page: int, progress=None) -> bytes:
        """Flash/EEPROM блоком: один запрос, Nano отдаёт страницы подряд (прошивка Nano v4+).
        progress(прочитано байт) — после каждой страницы."""
        out = bytearray()
        if self.version < 4:                       # старая прошивка Nano — по странице
            while len(out) < n:
                out += self.isp_read(space, addr + len(out), page)
                if progress:
                    progress(len(out))
            return bytes(out)
        self._send(CMD_ISP_READ_BLOCK, bytes([space, addr & 0xFF, addr >> 8, n & 0xFF, n >> 8]))
        while len(out) < n:
            status, data = self._receive(CMD_ISP_READ_BLOCK)
            if status != ST_OK:
                raise DeviceError(status, f"Чтение ISP 0x{addr + len(out):04X}")
            # страница больше 128 Б (S52) приходит двумя ответами
            if not data or len(out) + len(data) > n:
                raise IOError("Nano прислала страницу неверной длины")
            out += data
            if progress:
                progress(len(out))
        return bytes(out)

    def isp_write_page(self, space: int, addr: int, data: bytes):
        self._cmd(CMD_ISP_WRITE, bytes([space, addr & 0xFF, addr >> 8]) + data,
                  what=f"Запись ISP 0x{addr:04X}")

    def isp_write_part(self, space: int, addr: int, part: int, data: bytes):
        """Страница S52 (256 Б) двумя пакетами: part 0 — первая половина, 1 — вторая."""
        self._cmd(CMD_ISP_WRITE_PART, bytes([space, addr & 0xFF, addr >> 8, part]) + data,
                  what=f"Запись ISP 0x{addr:04X}")

    def isp_write_byte(self, space: int, addr: int, value: int):
        """Запись одного байта (Byte Mode, команда 0x40) — прошивка Nano v9+."""
        self._cmd(CMD_ISP_WRITE_BYTE, bytes([space, addr & 0xFF, addr >> 8, value]),
                  what=f"Запись байта 0x{addr:04X}")

    def isp_lock(self, level: int):
        self._cmd(CMD_ISP_LOCK, bytes([level]), what="Запись lock")

    def isp_erase(self):
        self._cmd(CMD_ISP_ERASE, timeout=5.0, what="Стирание AT89")

    # --- сниффер 1-Wire (прошивка Nano v5+) ---

    def sniff_start(self):
        """Запускает захват. Дальше Nano сама шлёт поток записей (firmware/include/sniff.h);
        читать sniff_read(), остановить sniff_stop(). Другие команды в это время слать нельзя."""
        if self.version < 5:
            raise IOError("Сниффер требует прошивку Nano v5 или новее")
        self._cmd(CMD_SNIFF, what="Сниффер")

    def sniff_read(self) -> bytes:
        """Всё, что пришло от Nano к этому моменту (ждёт не дольше таймаута порта)."""
        return self.ser.read(self.ser.in_waiting or 1)

    def sniff_stop(self, timeout: float = 3.0) -> bytes:
        """Останавливает захват. Возвращает остаток потока до маркера конца (0xFF)."""
        self.ser.write(b"\x00")
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buf += self.ser.read(self.ser.in_waiting or 1)
            end = buf.find(b"\xFF")
            if end >= 0:
                tail = bytes(buf[:end])
                rest = buf[end + 1:]
                while len(rest) < 4 and time.monotonic() < deadline:      # ответ ST_OK: 5A 00 00 00
                    rest += self.ser.read(4 - len(rest))
                if rest[:1] != b"\x5A":
                    raise IOError("Nano не подтвердила остановку сниффера")
                return tail
        raise TimeoutError("Nano не остановила сниффер")
