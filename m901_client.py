#!/usr/bin/env python3
"""
M901 (ASUS ROG Azoth 96 HE) - Python-клиент vendor-протокола.

Транспорт: HID, usage page 0xFF00 (USB) или 0xFF01, 64-байтные репорты без report ID.
Зависимости: pip install hidapi  (import hid)

Статус команд см. в analysis/PROTOCOL.md.
"""
from __future__ import annotations
import struct
import sys
import threading
import time

# Хук журналирования транзакций: (cmd, sub, echo, ответ|None, nak, мс).
# Прототип включает его в режиме --evt-dump для корреляции NAK с событиями.
TXLOG = None

try:
    import hid
except ImportError:
    raise SystemExit("pip install hidapi")

VID = 0x0B05
PID = 0x1C10           # APP-режим (bootloader - другой PID, не входит в поставку)
USAGE_PAGES = (0xFF00, 0xFF01)

# Windows-hidapi требует первый байт буфера = report ID (у устройства его
# нет → 0x00); Linux/hidraw шлёт репорт как есть. Проверено живой
# транзакцией 2026-10-04: без префикса на Windows устройство отвечает
# NAK FF AA 00 00 (пакет «съехал» на байт).
WRITE_PREFIX = b"\x00" if sys.platform == "win32" else b""

MAGIC_UNLOCK = bytes([0x7B, 0xAA, 0x41, 0x53, 0x55, 0x53, 0xAA]) + b"\x00" * 57  # "{AA ASUSS AA"

# Коды ответов
R_STATUS = 0x12
R_MATRIX = 0x24
R_LED    = 0x25
R_BOOL   = 0x27
R_CFG1   = 0x41
R_CFG2   = 0x43
R_SETACK = 0x51
R_NAK_HDR = b"\xff\xaa"


class M901:
    def __init__(self, path: bytes | None = None):
        self.dev = hid.device()
        if path:
            self.dev.open_path(path)
        else:
            opened = False
            for d in hid.enumerate(VID, PID):
                if d["usage_page"] in USAGE_PAGES:
                    self.dev.open_path(d["path"])
                    opened = True
                    break
            if not opened:
                raise IOError("vendor-интерфейс (usage page 0xFF00/0xFF01) не найден")
        self.dev.set_nonblocking(False)
        self._echo = 0
        self._tlock = threading.Lock()

    def close(self):
        self.dev.close()

    # ---------- транспорт ----------
    def _next_echo(self) -> int:
        self._echo = (self._echo + 1) & 0xFFFF
        return self._echo

    def send(self, cmd: int, sub: int, args: bytes = b"", echo: int | None = None) -> None:
        """CMD [SUB] [ECHO u16] [ARGS...] + паддинг до 64."""
        e = self._next_echo() if echo is None else echo
        pkt = struct.pack("<BBH", cmd, sub, e) + args
        pkt += b"\x00" * (64 - len(pkt))
        assert len(pkt) == 64
        self.dev.write(WRITE_PREFIX + pkt)

    def recv(self, timeout_ms: int = 1000) -> bytes | None:
        # ВАЖНО (Windows): timeout_ms=0 в cython-hidapi здесь означает
        # БЕСКОНЕЧНОЕ блокирующее чтение (проверено 2026-10-04) — переводим
        # в 1 мс, чтобы «неблокирующий» дренировочный вызов не зависал.
        if timeout_ms <= 0:
            timeout_ms = 1
        data = self.dev.read(64, timeout_ms=timeout_ms)
        return bytes(data) if data else None

    def transact(self, cmd: int, sub: int, args: bytes = b"", timeout_ms: int = 1000,
                 echo: int | None = None) -> bytes | None:
        """Послать команду и получить подходящий ответ (по echo).
        После вызова self.last_nak=True, если девайс ответил настоящим
        NAK `FF AA` (в т.ч. «гейт дисплея закрыт»), False при таймауте.
        Потокобезопасно: ридер-поток качельки пушит 51 0C параллельно
        с главным циклом — лок сериализует send+recv-цикл целиком."""
        t0 = time.monotonic()
        with self._tlock:
            e = self._next_echo() if echo is None else echo
            self.send(cmd, sub, args, echo=e)
            self.last_nak = False
            r = None
            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline:
                r = self.recv(200)
                if not r:
                    continue
                if r[:2] == R_NAK_HDR:
                    rcmd = struct.unpack_from("<H", r, 2)[0]
                    if rcmd == (cmd | (sub << 8)) or rcmd == cmd:
                        self.last_nak = True
                        break
                    continue
                rcmd, rsub, recho = r[0], r[1], struct.unpack_from("<H", r, 2)[0]
                # ВАЖНО (живой тест 2026-10-05): клавиатура шлёт несолицитированные
                # броадкасты 12 00/12 01 (эхо-байты 0000). Без проверки cmd транзакции
                # с echo=0 (51 0C и вся виджет-семья) фальшиво матчатся на них,
                # очередь ответов сдвигается, и пуши выглядят как «NAK/нет ответа».
                if recho == e and rcmd == cmd and rsub == sub:
                    break
                r = None
            else:
                r = None
        if TXLOG:
            try:
                TXLOG(cmd, sub, e, r, self.last_nak, (time.monotonic() - t0) * 1000)
            except Exception:
                pass
        return r

    # ---------- системные ----------
    def unlock(self) -> bool:
        """Магический код (нужен для 0x61/0x62/0x67/0x6B/0xC0).

        ВНИМАНИЕ: по данным реверса устройство после магии уходит в bootloader
        (reboot с флагом 0xDEADBEEF) - это путь обновления прошивки!
        """
        self.send(0x7B, 0xAA, b"ASUS\xAA")
        return True

    def get_connection_state(self) -> int | None:
        """0x12/0x00: 1 байт - состояние подключения (USB/BT/RF)."""
        r = self.transact(0x12, 0x00, b"")
        return r[4] if r and len(r) >= 5 else None

    def get_battery_flags(self) -> int | None:
        """0x12/0x02: зарядка/батарея (биты 0x30 из [0x23006F70])."""
        r = self.transact(0x12, 0x02, b"")
        return r[4] if r and len(r) >= 5 else None

    def get_pair_state(self) -> int | None:
        """0x12/0x06: состояние пейринга (2 = paired, 0xFF = no)."""
        r = self.transact(0x12, 0x06, b"")
        return r[4] if r and len(r) >= 5 else None

    # ---------- дисплей/виджеты (см. analysis/PROTOCOL_STATUSBAR.md, PROTOCOL_OLED.md) ----------
    def set_widget(self, slot: int, on: bool, mode: int = 0x00) -> bool:
        """0x6A sub 0: включить/выключить слот-виджет (бит slot маски 0x23008BCE).

        slot 0..4, mode - байт pkt[4]-окружения (в захватах 0x00/0x03).
        Выключение текущего слота автоматически переключает на следующий включённый.
        """
        assert 0 <= slot <= 4
        r = self.transact(0x6A, 0x00, bytes([slot, 0x01 if on else 0x00]))
        return bool(r) and r[0] == 0x6A

    def select_slot(self, slot: int) -> bool:
        """0x6A sub 1: сделать слот активным (только если его бит включён).

        Для выключенного слота - молча игнор; slot==0 дополнительно сбрасывает
        счётчик баннера [0x23014C34] (PROTOCOL_OLED.md §3).
        """
        assert 0 <= slot <= 4
        r = self.transact(0x6A, 0x01, bytes([slot, 0x00]))
        return bool(r) and r[0] == 0x6A

    def get_status_flags(self) -> list[int] | None:
        """0x24 sub 0x02: маска включённых слотов-виджетов.

        Возвращает 5 элементов (биты 0..4 байта 0x23008BCE), каждый 0/1:
        слот 0 = часы/календарь, 1/2 = индикаторы (тогглы качелькой / 0x64),
        3 = двойной индикатор CPU+RAM, 4 = особый слот. Бит 5 устройством
        не передаётся (резерв).
        """
        r = self.transact(0x24, 0x02, b"")
        if r and r[0] == R_MATRIX and r[1] == 0x02:
            return list(r[4:9])
        return None

    def get_current_slot(self) -> int | None:
        """0x24 sub 0x01: текущий выбранный слот (0..4)."""
        r = self.transact(0x24, 0x01, b"")
        if r and r[0] == R_MATRIX and r[1] == 0x01:
            return r[4]
        return None

    def get_screen_compact(self) -> tuple[int, int] | None:
        """0x24 sub 0x00: «что на экране» — {slot<<4|флаги, под-состояние}.

        Та же кодировка, что IPC 0x95/0x96 к sysctrl (PROTOCOL_STATUSBAR.md §7):
        байт 0 - слот в старшем ниббле + флаги режима; байт 1 - под-состояние
        (для слота часов - нибблы времени и т.п.).
        """
        r = self.transact(0x24, 0x00, b"")
        if r and r[0] == R_MATRIX and r[1] == 0x00:
            return (r[4], r[5])
        return None

    def set_widget_slot(self, slot: int, on: bool) -> bool:
        """0x6A sub 0: включить/выключить слот-виджет (бит slot маски 0x23008BCE).

        slot 0..4; выключение текущего слота переключает девайс на следующий
        включённый (видно по get_current_slot). Совпадает с set_widget().
        """
        return self.set_widget(slot, on)

    def select_widget_slot(self, slot: int) -> bool:
        """0x6A sub 1: сделать слот активным (если его бит включён).

        GearLink-паттерн «показать виджет»: set_widget_slot(slot, True) +
        select_widget_slot(slot). Для выключенного слота - молча игнорируется.
        """
        return self.select_slot(slot)

    def slideshow(self, slots: list[int], interval: float = 5.0, cycles: int = 0) -> None:
        """Слайдшоу виджетов: циклически переключает OLED по включённым слотам
        (6A 01, подтверждено живым тестом 2026-10-03 — GearLink так показывает
        настраиваемый виджет). slots = слоты для ротации, interval = секунды
        на кадр, cycles = число полных кругов (0 = бесконечно, Ctrl+C для стопа).
        Слоты предварительно включить: set_widget(s, True) для s из slots."""
        import time as _t
        assert all(0 <= s <= 4 for s in slots) and slots
        n = 0
        try:
            while True:
                for s in slots:
                    self.select_slot(s)
                    _t.sleep(interval)
                n += 1
                if cycles and n >= cycles:
                    break
        except KeyboardInterrupt:
            pass

    def get_current_page(self) -> int | None:
        """0x21 sub 0: текущая страница экрана [0x23000C9F]."""
        r = self.transact(0x21, 0x00, b"")
        if r and r[0] == 0x21:
            return r[4]
        return None

    def get_screen_state(self) -> int | None:
        """0x23 sub 0x02: байт «экран включён» [0x230086BC+0x513]."""
        r = self.transact(0x23, 0x02, b"")
        if r and r[0] == 0x23:
            return r[4]
        return None

    def screen_is_on(self) -> bool:
        """True, если экран явно включён. None/ошибка чтения трактуется как
        «включён» — будить можно только по ЯВНОму нулю, иначе ложные
        пробуждения (блинки) на занятом тракте (OSD качельки, жесты)."""
        s = self.get_screen_state()
        return s is None or s != 0

    def get_status_struct(self) -> bytes | None:
        """0x12 sub 0 (echo=0): 16-байтный дамп 0x23000C98.

        +0 u16 CPU temp, +2 u16 CPU %, +4 u16 RAM %, +6 dev, +7 page,
        +9 slot, +10 флаг 0xB5.
        """
        r = self.transact(0x12, 0x00, echo=0)
        if r and r[0] == R_STATUS:
            return bytes(r[4:20])
        return None

    def get_battery(self) -> int | None:
        """0x12 sub 1: заряд батареи % - payload[1] ([0x2301D26D])."""
        r = self.transact(0x12, 0x01, b"")
        if r and r[0] == R_STATUS and r[1] == 0x01:
            return r[5]
        return None

    # ---------- OLED: время, метрики, яркость, пробуждение (PROTOCOL_OLED.md) ----------
    def set_clock(self, year: int, month: int, day: int, hour: int, minute: int) -> bool:
        """0x63: установить дату/время (как GearLink в конфиг-пачке).

        Провод: `63 00 <echo> 00 <date u32 LE> <time u16 LE>`, где
        date = year | month<<16 | day<<24, time = hour | minute<<8
        (захват 08: `63 00 00 00 00 EA 07 0A 02 11 35` = 2026-10-02 17:53).
        Заодно взводит бит1 [0x23014B49] («время поставлено»).
        """
        assert 1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60
        date = (year & 0xFFFF) | (month << 16) | (day << 24)
        tval = (hour & 0xFF) | ((minute & 0xFF) << 8)
        args = b"\x00" + struct.pack("<IH", date, tval)
        r = self.transact(0x63, 0x00, args, echo=0)
        return bool(r) and r[0] == 0x63

    def send_cpu_usage(self, usage: int, temp: int, sel2: int = 2, val2: int = 0) -> bool:
        """0x66 (echo=1): пуш метрик — две пары {селектор u8, rsv u8, значение u16 LE}.

        Формат по захватам (08b/09, байт-в-байт):
        `66 00 01 00 | 00 00 <usage u16> | 01 00 <temp u16>`
        sel 0 = usage %, sel 1 = temp °C (пространство селекторов 0..5,
        нибблы: hi ≤ 4, lo ≤ 5). ВАЖНО (исправлено 2026-10-03): раньше
        байт 7 трактовался как «RAM %» — это неверно, это старший байт
        u16 usage (в захватах всегда 00; запись туда ненулевого байта
        портит значение usage: 50%+10*256 = 2610%).
        """
        assert 0 <= usage <= 100 and temp < 65536 and sel2 <= 5 and val2 < 65536
        args = (bytes([0x00, 0x00]) + struct.pack("<H", usage) +
                bytes([sel2, 0x00]) + struct.pack("<H", temp if sel2 == 1 else val2))
        r = self.transact(0x66, 0x00, args, echo=1)
        return bool(r) and r[0] == 0x66

    def set_slot2_value(self, value: int, on: int = 0) -> bool:
        """0x64: значение тайла слота 2 + вкл/выкл (подтверждено захватом
        10-battery-toggle: GearLink шлёт `64 00 <echo> <батарея %> 00` —
        виджет батареи хоста = слот 2, значение = проценты)."""
        assert 0 <= value <= 255
        r = self.transact(0x64, 0x00, bytes([value, on]), echo=0)
        return bool(r) and r[0] == 0x64

    def send_metric(self, sel: int, digit: int, value: int) -> bool:
        """0x66 (echo=1) с произвольным селектором: sel lo-ниббл = тип значения
        (0=Usage %, 1=Temp °C, 2=Freq, 5=Volt, 3/4=Fan — по захвату
        11-mon-widgets), hi-ниббл = заголовок тайла (0=CPU, 1=GPU, 2=VRM,
        3=DRAM, 4=CHA), digit = цифра инстанса (0xFF = без цифры).
        Полезно для теста заголовков: sel=0x31 -> «DRAM0 / Temp.».
        Вторая пара — нейтральная {00 00 0000}, как в одиночных пушах GearLink."""
        args = (bytes([sel & 0xFF, digit & 0xFF]) + struct.pack("<H", value) +
                bytes([0x00, 0x00]) + struct.pack("<H", 0))
        r = self.transact(0x66, 0x00, args, echo=1)
        return bool(r) and r[0] == 0x66

    def push_metrics(self, pairs, echo: int = 0) -> bool:
        """0x66: пуш 1-2 пар {sel u8, digit u8, value u16 LE} — общий вариант
        send_cpu_usage/send_metric. sel: lo-ниббл = подпись значения
        (0=Usage, 1=Temp., 2=Freq., 5=Volt), hi-ниббл = заголовок тайла
        (0=CPU, 1=GPU, 2=VRM, 3=DRAM, 4=CHA); валидация хендлера hi≤4, lo≤5
        (PROTOCOL_OLED.md §10.3.2). digit = цифра инстанса (0 → «CPU0»/«DRAM0»,
        0xFF = без цифры). Одиночный пуш дополняется нейтральной второй парой
        {00 00 0000}, как в захватах GearLink.
        ВАЖНО (живой тест 2026-10-04): у 0x66 два пути по echo (§4) —
        echo=1 → 0x0E08D65A («шумный», при каждом пуше экран прыгает на
        первую страницу карусели слота 4), echo=0 → тихий IPC 0x1E.
        Слайдшоу GearLink шлёт одиночные пуши с echo=0 (захват 20),
        конфиг-пуши — с echo=1. По умолчанию тут echo=0."""
        assert 1 <= len(pairs) <= 2
        args = b""
        for sel, digit, value in pairs:
            hi, lo = (sel >> 4) & 0xF, sel & 0xF
            assert hi <= 4 and lo <= 5 and 0 <= value <= 0xFFFF
            args += bytes([sel & 0xFF, digit & 0xFF]) + struct.pack("<H", value)
        if len(pairs) == 1:
            args += b"\x00\x00\x00\x00"
        r = self.transact(0x66, 0x00, args, echo=echo)
        return bool(r) and r[0] == 0x66

    def push_volume_osd(self, v: int) -> bool:
        """`51 0C 00 00 [V]` — OSD громкости «V%» (PROTOCOL_VOLUME.md §2).
        echo ОБЯЗАТЕЛЬНО 0x0000: это OSD-ветка (счётчик [0x23006F68]=1000,
        IPC 0x26 + таймер скрытия IPC 0x16); echo=1 уходит в write-only
        копию «последней громкости» без OSD. Скрытие через 1 с делает
        сама клавиатура — живёт OSD только на тиках качельки."""
        v = max(0, min(100, int(v)))
        r = self.transact(0x51, 0x0C, bytes([v]), echo=0)
        return bool(r) and r[0] == 0x51

    def wake_display(self, mode: int = 0xFF) -> bool:
        """0x65: «показать/разбудить» OLED (IPC 0x1F, флаг [0x2301D249]=1).

        GearLink в захватах (16/18) шлёт mode=0xFF — «просто показать/разбудить».
        Режим 0 при живом тесте 2026-10-04 переключил экран на вид
        уведомлений (тайл «Mail») — не использовать без нужды. Флаг [0x2301D249]
        ещё и гейтит жесты тачскрина, пока не протухнет.
        """
        assert mode == 0xFF or mode <= 2
        r = self.transact(0x65, 0x00, bytes([mode]), echo=0)
        return bool(r) and r[0] == 0x65

    def screen_on(self, on: bool = True) -> bool:
        """0x69: включить экран ([0x230086BC+0x513]=1 при pkt[4]!=0)."""
        r = self.transact(0x69, 0x00, bytes([0x01 if on else 0x00]), echo=0)
        return bool(r) and r[0] == 0x69

    def set_brightness(self, value: int) -> bool:
        """0x68: яркость 0..100 (в захватах 100 в конце пачки применения)."""
        assert 0 <= value <= 100
        r = self.transact(0x68, 0x00, bytes([value, 0x00]), echo=0)
        return bool(r) and r[0] == 0x68

    def set_page(self, page: int) -> bool:
        """0x61 sub 0: сменить страницу экрана (0..5; IPC {0x13, dev, page})."""
        assert page <= 5
        r = self.transact(0x61, 0x00, bytes([page]), echo=0)
        return bool(r) and r[0] == 0x61

    def commit(self) -> bool:
        """`50 55 00 00`: фиксация пачки - применить RGB-стейт + вытолкнуть
        FB-очередь на iface2 IN (PROTOCOL_OLED.md §5). Завершает конфиг-пачку
        GearLink-стиля. Ответ - эхо `50 55`."""
        r = self.transact(0x50, 0x55, b"", echo=0)
        return bool(r) and r[0] == 0x50 and r[1] == 0x55

    def gearlink_session(self, on: bool) -> bool:
        """0x74 sub 0: флаг «vendor-сессия активна» [0x2301D21C].

        on=True разрешает `C0 81` (чтение памяти через sysctrl); 0x27 -
        просто чтение этого флага. Не путать с unlock() (магия -> bootloader!).
        """
        r = self.transact(0x74, 0x00, bytes([0x01 if on else 0x00]), echo=0)
        return bool(r) and r[0] == 0x74

    def ping_gearlink(self) -> bool | None:
        """0x27: прочитать флаг vendor-сессии ([0x2301D21C])."""
        r = self.transact(0x27, 0x00, b"")
        if r and r[0] == R_BOOL:
            return bool(r[4])
        return None

    # ---------- OLED: заливка картинки 184x97 RGB565 (PROTOCOL_OLED.md §3-4) ----------
    PANEL_W = 184
    PANEL_H = 97
    CHUNK_PAYLOAD = 58          # байт битмап-потока на пакет `61 02`
    BITMAP_META = b"\x01\x00\xe8\x03"   # фикс. заголовок потока (см. §3.5)

    @staticmethod
    def rgb565_le(r: int, g: int, b: int) -> bytes:
        """Пиксель RGB888 -> RGB565 little-endian (2 байта, младший первым)."""
        px = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return struct.pack("<H", px)

    def upload_image(self, pixels: bytes, width: int = PANEL_W, height: int = PANEL_H,
                     brightness: int = 100, select: bool = True,
                     progress=None) -> int:
        """Залить кастомную картинку на OLED (протокол захватов 06-custom-image).

        pixels: width*height пикселей RGB565 **LE** (2 байта/пиксель, младший
        байт первым - порядок подтверждён по сплэшу, PROTOCOL_OLED.md §4).
        Последовательность: `6A 00 [0] 01` (вкл. слот 0) -> `6B 00` ->
        `61 01 <n u32 LE>` -> n×`61 02 <idx LE u32>` (idx от n-1 к 0, по 58 Б
        потока; поток = мета `01 00 e8 03` + пиксели + паддинг) -> `61 03` ->
        `6A 01` (выбор слота 0) -> `68` (яркость) -> `50 55` (commit).
        Возвращает число отправленных чанков. progress(i, n) - опциональный колбэк.
        """
        assert len(pixels) == width * height * 2, "нужен RGB565 LE-буфер w*h*2"
        stream = bytearray(self.BITMAP_META + pixels)
        n = (len(stream) + self.CHUNK_PAYLOAD - 1) // self.CHUNK_PAYLOAD
        stream += b"\x00" * (n * self.CHUNK_PAYLOAD - len(stream))
        self.set_widget(0, True)
        self.send(0x6B, 0x00, b"")
        self.send(0x61, 0x01, struct.pack("<I", n))
        sent = 0
        # порядок как в захвате: данные льются по порядку потока (мета - в первом
        # пакете), а индекс чанка в echo УБЫВАЕТ от n-1 к 0 (устройство только
        # проверяет монотонность и пушит данные в кольцо в порядке прибытия)
        for pos in range(n):
            idx = n - 1 - pos
            chunk = stream[pos * self.CHUNK_PAYLOAD:(pos + 1) * self.CHUNK_PAYLOAD]
            self.send(0x61, 0x02, b"\x00\x00" + chunk, echo=idx)
            sent += 1
            if progress and (sent % 32 == 0 or idx == 0):
                progress(sent, n)
            if sent % 16 == 0:
                self.recv(0)            # дренировать ACK-канал
        self.send(0x61, 0x03, b"")
        if select:
            self.select_slot(0)
        self.set_brightness(brightness)
        self.commit()
        return sent

    def banner_begin(self, width: int, height: int = 48) -> bool:
        """0x67 sub 0 (arg=2): начать сессию «баннера» слота 0.

        width 136..680, height обязан быть 48. Чанки дальше - banner_chunk().
        `67 00 <echo> 01` - отмена (banner_cancel)."""
        assert 136 <= width <= 680 and height == 48
        args = bytes([0x02]) + struct.pack("<HH", width, height)
        r = self.transact(0x67, 0x00, args)
        return bool(r) and r[0] == 0x67

    def banner_chunk(self, idx: int, data: bytes) -> bool:
        """0x67 sub 1: чанк баннера, idx убывает от 47 к 0 (echo = idx)."""
        assert len(data) == self.CHUNK_PAYLOAD
        self.send(0x67, 0x01, b"\x00\x00" + data, echo=idx)
        return True

    def banner_commit(self, params: bytes = b"\x00" * 28) -> bool:
        """0x67 sub 2: коммит баннера -> [0x23014C34]=1 (спец-рендер слота 0),
        автоскрытие через 30 тиков; params - 28 байт pkt[4..0x1F]."""
        assert len(params) <= 28
        r = self.transact(0x67, 0x02, params.ljust(28, b"\x00"))
        return bool(r) and r[0] == 0x67

    def banner_cancel(self) -> bool:
        """0x67 sub 0 (arg=1): сбросить баннер слота 0."""
        r = self.transact(0x67, 0x00, bytes([0x01]))
        return bool(r) and r[0] == 0x67


    # ---------- RGB ----------
    @staticmethod
    def rgb12_to_rgb888(c12: int) -> tuple[int, int, int]:
        r = (c12 >> 8) & 0xF
        g = (c12 >> 4) & 0xF
        b = c12 & 0xF
        return (r << 4) | r, (g << 4) | g, (b << 4) | b

    @staticmethod
    def rgb888_to_rgb12(r: int, g: int, b: int) -> int:
        return ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)

    def get_key_color(self, key: int, layer: int = 0) -> tuple[int, int, int] | None:
        """0x25/0x04: цвет одной клавиши. key - LED/HID-индекс (см. luts.json).

        (В старых версиях клиента и доков ошибочно значилось как «0xFD 0x04» -
        на самом деле get-семейство RGB - это опкод 0x25.)
        Формат подтверждён дизассемблером 0x0E07BC18 2026-10-03:
        echo есть, key@offset 4, layer@offset 5 (0x00/0x9F) - в отличие от
        set-color, у которого echo нет и [key][layer] лежат на offset 2..3.
        """
        layer_arg = 0x9F if layer else 0x00
        r = self.transact(0x25, 0x04, bytes([key & 0xFF, layer_arg]))
        if r and r[0] == R_LED and len(r) >= 6:
            c12 = struct.unpack_from("<H", r, 4)[0] & 0xFFF
            return self.rgb12_to_rgb888(c12)
        return None

    def set_key_color(self, key: int, r8: int, g8: int, b8: int,
                      layer: int = 0, time10: int = 0, sub: int = 0x21,
                      spec: int | None = None) -> bool:
        """0x51 sub 0x21/0x22: цвет ОДНОЙ клавиши, 12 бит (set-color 0x0E07BFC0).

        Исправлено 2026-10-03 по дизассемблеру 0x0E07BFC0: в set-color НЕТ поля
        echo - пакет [51][21/22][key][layer][spec u16][color u16][time u16]:
        key на offset 2 (0..0xBC или спец-код 0xD3), layer на offset 3
        (0x00 = слой 0, 0x9F = слой 1/Fn). «Range» из старой версии - это spec,
        кодировка немедленной записи в таблицу цветов (по умолчанию spec =
        color: при >= 0x306 это прямой 12-битный цвет); цвет записи профиля
        всегда = pkt[6..7]. Ack 0x51 в поле echo несёт key | layer<<8, поэтому
        transact() с матчингом по echo тут не годится.
        sub 0x21 не трогает time, sub 0x22 записывает ещё и time (pkt[8]/10).
        """
        layer_arg = 0x9F if layer else 0x00
        c12 = self.rgb888_to_rgb12(r8, g8, b8)
        if spec is None:
            spec = c12
        pkt = struct.pack("<BBBBHHH", 0x51, sub & 0xFF, key & 0xFF, layer_arg,
                          spec & 0xFFFF, c12, (time10 * 10) & 0xFFFF)
        pkt += b"\x00" * (64 - len(pkt))
        self.dev.write(WRITE_PREFIX + pkt)
        want = (key & 0xFF) | (layer_arg << 8)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            r = self.recv(200)
            if not r:
                continue
            if r[:2] == R_NAK_HDR:
                return False
            if r[0] == R_SETACK and r[1] == (sub & 0xFF) and \
               struct.unpack_from("<H", r, 2)[0] == want:
                return True
        return False

    # ---------- bootloader ----------
    def enter_bootloader(self):
        """Отправляет magic и ждёт переперечисления (device отключится)."""
        self.unlock()

    # ---------- громкость / OSD (см. analysis/PROTOCOL_VOLUME.md) ----------
    def send_volume_osd(self, volume: int, store: bool = False) -> bool:
        """0x51 sub 0x0C: отрисовать OSD громкости на OLED качельки.

        volume - проценты 0..100 (в захвате шаг 2 за щелчок качельки).
        echo=0 (по умолчанию) - нарисовать сейчас (IPC 0x26 "V%" + IPC 0x16,
        автоскрытие через ~1 c; работает при [0x23000CA0]==0).
        store=True (echo=1) - только сохранить проценты в [0x23013A4C+2].
        Ответ - эхо `51 0C <echo> <V>`.
        """
        assert 0 <= volume <= 100
        r = self.transact(0x51, 0x0C, bytes([volume]), echo=1 if store else 0)
        return bool(r) and r[0] == R_SETACK and r[1] == 0x0C

    def get_key_assignment(self, page: int, usage: int) -> int | None:
        """0x25 sub 0x0C: привязка спец-клавиши качельки на странице профиля.

        НЕ громкость! page 0..0x0A; usage - HID usage спец-клавиши:
        0xA3..0xA6 -> матрица 0xD3..0xD6 (качелька). Возвращает сохранённый
        код назначения (в захвате 488/489 = vol-/vol+, 498 = push?) как u16;
        None при NAK.
        """
        assert 0 <= page <= 0x0A
        args = struct.pack("<BH", page, usage)
        r = self.transact(0x25, 0x0C, args)
        if r and r[0] == R_LED and r[1] == 0x0C:
            return struct.unpack_from("<H", r, 4)[0]
        return None

    def open_consumer(self):
        """Открыть iface2 (consumer control, usage page 0x0C) для чтения
        IN-событий EP 0x83: `01 00 <bitmap20>` и vendor `03 7x/03 91`."""
        for d in hid.enumerate(VID, PID):
            if d["usage_page"] == 0x0C:
                dev = hid.device()
                dev.open_path(d["path"])
                dev.set_nonblocking(False)
                return dev
        raise IOError("consumer-интерфейс (usage page 0x0C) не найден")

    def open_ffc0(self):
        """Открыть канал 0xFFC0 (Col03 iface2, 20-байтные репорты Report ID 3).

        Туда падает зеркало событий клавиатуры: `03 93 <slot>` (смена слота),
        `03 95/96 00 00 30` (статус виджета / локальное изменение — эмпирически
        2026-10-04: один `03 96` на каждый принятый вертикальный свайп),
        `03 71 <матрица тача>` (данные тачскрина). ВНИМАНИЕ: на Windows у
        iface2 три коллекции с разными device-path'ами — хендл Col01 (consumer)
        этот стрим НЕ получает."""
        for d in hid.enumerate(VID, PID):
            if d["usage_page"] == 0xFFC0:
                dev = hid.device()
                dev.open_path(d["path"])
                dev.set_nonblocking(False)
                return dev
        raise IOError("0xFFC0-канал (usage page 0xFFC0) не найден")


def emulate_gearlink(kbd: M901, volume: int = 50, timeout_s: float | None = None) -> None:
    """Минимальная замена GearLink в громкостном цикле качельки.

    Читает EP 0x83 (iface2): тик вверх = consumer `01 00 04` и/или vendor `03 72 01`,
    вниз = `01 00 02` / `03 72 04`, отпускание = `01 00 00` / `03 72 00`.
    На каждый тик шлёт `51 0C 00 00 [V]` (шаг 2, как в захвате 03b-jogdial) -
    на OLED появляется OSD громкости. Ctrl+C для выхода.
    """
    cons = kbd.open_consumer()
    step = 2
    print("jogdial volume loop: V=%d%%, Ctrl+C для выхода" % volume)
    try:
        deadline = time.time() + timeout_s if timeout_s else None
        while True:
            data = cons.read(64, timeout_ms=200)
            if data:
                rid = data[0]
                if rid == 0x01 and len(data) >= 3:
                    bits = data[2]
                    if bits & 0x04:
                        delta = +step   # бит10 = Volume Increment (0xE9)
                    elif bits & 0x02:
                        delta = -step   # бит9 = Volume Decrement (0xEA)
                    else:
                        delta = 0       # отпускание (00) или посторонний бит
                    if delta:
                        volume = max(0, min(100, volume + delta))
                        kbd.send_volume_osd(volume)
                        print("consumer tick %s -> V=%d%%" % (
                            "up" if delta > 0 else "down", volume))
                elif rid == 0x03 and len(data) >= 3 and data[1] == 0x72:
                    names = {0x01: "up", 0x04: "down", 0x02: "press", 0x00: "release"}
                    print("vendor 03 72 %s" % names.get(data[2], hex(data[2])))
                elif rid == 0x03 and len(data) >= 5 and data[1] == 0x91:
                    print("vendor 03 91 phase=%d" % data[4])
            if deadline and time.time() > deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        cons.close()


def main():
    import sys
    dev = M901()
    print("connected:", dev.dev.get_product_string())
    print("status flags (0x24/0x02):", dev.get_status_flags())
    print("current slot (0x24/0x01):", dev.get_current_slot())
    print("current page (0x21/0x00):", dev.get_current_page())
    print("screen state (0x23/0x02):", dev.get_screen_state())
    print("battery (0x12/0x01):", dev.get_battery())
    st = dev.get_status_struct()
    if st:
        print("status struct (0x12/0x00): temp=%d cpu=%d ram=%d dev=%d page=%d slot=%d" % (
            int.from_bytes(st[0:2], "little"), int.from_bytes(st[2:4], "little"),
            int.from_bytes(st[4:6], "little"), st[6], st[7], st[9]))
    if len(sys.argv) > 1 and sys.argv[1] == "--boot":
        print("sending magic -> device reboots to bootloader")
        dev.enter_bootloader()
    elif len(sys.argv) > 1 and sys.argv[1] == "--volume":
        # громкостной цикл качельки без GearLink: python m901_client.py --volume [стартовый %]
        v0 = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        emulate_gearlink(dev, volume=v0)
    elif len(sys.argv) > 1 and sys.argv[1] == "--osd":
        # разовое OSD громкости: python m901_client.py --osd [0..100]
        v = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        print("volume OSD %d%% ->" % v, dev.send_volume_osd(v))
    elif len(sys.argv) > 1 and sys.argv[1] == "--jogdial-map":
        # привязки качельки (доказательство, что 25 0C - не громкость):
        # python m901_client.py --jogdial-map [page]
        page = int(sys.argv[2], 0) if len(sys.argv) > 2 else 9
        for usage in (0xA3, 0xA4, 0xA5, 0xA6):
            print("page %d usage 0x%02X -> %s" % (
                page, usage, dev.get_key_assignment(page, usage)))
    elif len(sys.argv) > 1 and sys.argv[1] == "--wake":
        # разбудить OLED без GearLink: python m901_client.py --wake
        print("wake_display ->", dev.wake_display())
    elif len(sys.argv) > 1 and sys.argv[1] == "--clock":
        # выставить текущее локальное время: python m901_client.py --clock
        t = time.localtime()
        print("set_clock ->", dev.set_clock(t.tm_year, t.tm_mon, t.tm_mday,
                                            t.tm_hour, t.tm_min))
    elif len(sys.argv) > 1 and sys.argv[1] == "--cpu":
        # пуш метрик: python m901_client.py --cpu [usage] [temp]
        u = int(sys.argv[2]) if len(sys.argv) > 2 else 42
        tp = int(sys.argv[3]) if len(sys.argv) > 3 else 55
        print("send_cpu_usage ->", dev.send_cpu_usage(u, tp))
    elif len(sys.argv) > 1 and sys.argv[1] == "--image":
        # залить сырой RGB565 LE-файл (w*h*2 байт): python m901_client.py --image frame.raw
        with open(sys.argv[2], "rb") as f:
            raw = f.read()
        w, h = 184, 97
        assert len(raw) >= w * h * 2, "файл короче 184*97*2"
        n = dev.upload_image(raw[:w * h * 2],
                             progress=lambda i, t: print("chunk %d/%d" % (i, t)))
        print("uploaded %d chunks" % n)


if __name__ == "__main__":
    main()
