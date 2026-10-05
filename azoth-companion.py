#!/usr/bin/env python3
"""
Azoth Companion v0.3 — прототип замены GearLink для ASUS ROG Azoth 96 HE (M901).

Модель протокола снята с живого захвата GearLink 2026-10-04
(C:/azoth-capture/16 и 18, расшифровка в README.md):

  • старт:            `65 FF` (wake) → опционально `6A`-включение слотов +
                       `68` яркость + `50 55` commit (ТОЛЬКО при смене маски);
  • значения (без commit): `66` CPU usage/temp → слот 3,
                       `64` батарея ПК % → слот 2,
                       `63` дата+время → слот 1 (циферблат рисует устройство);
  • слоты: 0 = баннер/music mode (не трогаем), 1 = часы, 2 = батарея,
                       3 = двойной индикатор, 4 = карусель мониторинга;
  • polling GearLink: `12 01`/`12 00` раз в ~30 с, `27` пинг — в v0.2 не критично.

Запуск:
  python azoth-companion.py                         # только баннер (слот 0) + OSD качельки
  python azoth-companion.py --clock --battery --monitor
                                            # классический набор GearLink
  python azoth-companion.py --monitor --slideshow 3 # слайдшоу cpu.usage → ram.usage → cpu.freq
  python azoth-companion.py --monitor-items cpu.usage,gpu.temp,freq
                                            # свой набор (включает слайдшоу, 2 с)
  python azoth-companion.py --monitor-items cpu.usage,temp --slideshow 5 --log-file
                                            # то же + дублировать лог в logs/azoth-companion.log
  python azoth-companion.py --install-autostart     # автозапуск при логоне (pythonw, планировщик)
  python azoth-companion.py --uninstall-autostart   # убрать задачу автозапуска
  python azoth-companion.py --once --cpu 42 --bat 77   # разовая проверка
  python azoth-companion.py --status                # только чтение статуса, ничего не менять
  python azoth-companion.py --demo --bat 42         # тест сенсоров: 0→100→0 (~5.5 с в сторону)

Виджеты = слоты: --banner (0), --clock (1), --battery (2), --monitor (3),
--kps (4); без флагов включён только баннер. Флаги контента включают свой
виджет автоматически: --metrics/--slideshow/--monitor-items/--demo/--cpu/
--temp/--ram-val → --monitor, --bat → --battery.

Graceful shutdown: Ctrl+C и обрабатываемые сигналы завершения (SIGTERM,
SIGBREAK, SIGHUP, SIGQUIT) гасят все виджеты, кроме баннера, — без демона
часы/батарея/метрики показывают протухшие данные. Офлайн живут только
баннер и KPS; пустая маска для OLED некорректна, поэтому если демона
запускали без баннера, при останове он включается обратно. --once/--status
раскладку не глушат (--once оставляет её на экране для сверки).

Слайды --monitor-items (формат «источник.метрика», сетка конфига GearLink):
  источники cpu / gpu / ram, метрики usage / temp / freq / fan / volt — например
  cpu.usage, cpu.temp, cpu.freq, cpu.volt, ram.usage, gpu.temp. Источник ram
  рисуется заголовком «DRAM0» (селектор 0x30), gpu — «GPU0» (0x10). Короткие
  имена первой редакции (cpu, gpu, ram, usage, temp, freq, volt) принимаются
  как алиасы. Сенсорные слайды (все temp/freq/volt и gpu.usage) требуют
  запущенный LibreHardwareMonitor — без сенсора слайд пропускается с одним
  предупреждением. Свайп вниз листает слайды вручную (пауза автолистания
  5 с); вверх прошивка хосту не сообщает.

Зависимости: pip install hidapi psutil
Температура/вольтаж CPU: запустить LibreHardwareMonitor.exe и pip install wmi
  (читаем WMI root\\LibreHardwareMonitor, сенсоры «CPU Package» и Voltage).
  Без них слайды temp/volt пропускаются, а --metrics cpu-temp пушит одиночную
  пару usage (fallback, как GearLink с одинарным виджетом). ACPI-термозоны на
  этой машине нет (проверено).
macOS (Apple Silicon): brew install macmon — температуры CPU/GPU, реальная
  частота и загрузка GPU без sudo (sudoless IOReport). Без него слайды
  temp/freq/gpu пропускаются.

GearLink перед запуском закрыть — два хозяина vendor-канала не нужны.
"""
from __future__ import annotations

import argparse
import ctypes                  # MacVolume: CoreAudio (WindowsVolume импортирует лениво)
import datetime as dt
import json
from collections.abc import Callable
from contextlib import suppress
import logging
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "analysis"))

from m901_client import M901  # noqa: E402  (нужен pip install hidapi)
import m901_client  # noqa: E402

# Слоты-виджеты (подтверждено маской GearLink в живой сессии 2026-10-04:
# минимальная конфигурация часы+батарея+CPU дала маску [0,1,1,1,0])
SLOT_BANNER = 0       # баннер / music mode / кастомный битмап — не трогаем
SLOT_CLOCK = 1        # часы: контент = push 0x63
SLOT_BATTERY = 2      # батарея ПК: контент = push 0x64 <проценты>
SLOT_MONITOR = 3      # ДВОЙНОЙ индикатор (два тайла): контент = push 0x66
SLOT_KPS = 4          # нативный KPS-тайл (клавиш/с): прошивка рисует сама,
                      # хост только включает слот (бывш. «карусель» — гипотеза
                      # «одиночный тайл» не подтвердилась, см. README.md)

# Виджеты = слоты, флаги --banner/--clock/--battery/--monitor/--kps.
# Без флагов включён только баннер: он статичен и без демона не протухает,
# в отличие от часов/батареи/метрик (см. shutdown_widgets).
WIDGET_FLAGS = (       # имя argparse-флага → слот
    ("banner", SLOT_BANNER),
    ("clock", SLOT_CLOCK),
    ("battery", SLOT_BATTERY),
    ("monitor", SLOT_MONITOR),
    ("kps", SLOT_KPS),
)
WIDGET_NAMES = {SLOT_BANNER: "баннер", SLOT_CLOCK: "часы",
                SLOT_BATTERY: "батарея", SLOT_MONITOR: "монитор",
                SLOT_KPS: "KPS"}
DEFAULT_SLOTS = (SLOT_BANNER,)   # набор при запуске без флагов

CLOCK_SYNC_S = 60.0     # не используется для таймера: часы синхронизируются
                        # на границе каждой минуты (см. run)
HEARTBEAT_S = 10.0      # дисплей засыпает после ~30 тиков простоя: пушим
                        # значения безусловно раз в 10 с, чтобы экран жил
                        # (иначе NAK + мигание после каждого пробуждения)
SWIPE_PAUSE_S = 5.0     # после ручного свайпа автолистание встаёт на паузу
WAKE_EVERY_S = 60.0
STAT_EVERY_S = 30.0

# Слайды слайдшоу (v0.3): сетка «источник.метрика» из конфига GearLink —
# она же раскладка нибблов селектора 0x66 (PROTOCOL_OLED.md §10.5):
# hi-ниббл = заголовок тайла {0=CPU, 1=GPU, 2=VRM, 3=DRAM, 4=CHA},
# lo-ниббл = подпись значения {0=Usage, 1=Temp., 2=Freq., 3/4=Fan, 5=Volt}.
# GearLink конфигурирует только cpu/gpu/ram × usage/temp/volt/freq —
# VRM/CHA в прошивке есть, но в его сетке отсутствуют, не выставляем.
SLIDE_SOURCES = {"cpu": 0x0, "gpu": 0x1, "ram": 0x3}      # «ram» = заголовок DRAM
SLIDE_METRICS = {"usage": 0x0, "temp": 0x1, "freq": 0x2, "fan": 0x3, "volt": 0x5}
SLIDE_ALIASES = {   # короткие имена первой редакции v0.3 → канонические
    "cpu": "cpu.usage", "gpu": "gpu.usage", "ram": "ram.usage",
    "usage": "cpu.usage", "temp": "cpu.temp", "freq": "cpu.freq",
    "fan": "cpu.fan", "volt": "cpu.volt",
}
DEFAULT_SLIDES = ("cpu.usage", "ram.usage", "cpu.freq")   # набор v0.2: CPU0 Usage /
                                                          # DRAM0 Usage (RAM) / CPU0 Freq
DEFAULT_SLIDESHOW_S = 2.0                 # период, если --monitor-items задан без --slideshow
AUTOSTART_TASK = "Azoth Companion"                # имя задачи планировщика (текущий пользователь)
LOG_MAX_BYTES = 2 * 1024 * 1024           # ротация лог-файла: ~2 МБ
LOG_BACKUPS = 2                           # итого 3 файла: azoth-companion.log, .1, .2

# Graceful shutdown: обрабатываемые сигналы завершения ставят STOP, главный
# цикл выходит, и перед закрытием устройства гасятся все виджеты, кроме
# баннера (shutdown_widgets) — часы/батарея/метрики без демона показывают
# протухшие данные. SIGINT не трогаем: Ctrl+C штатно приходит как
# KeyboardInterrupt. На Windows SIGTERM снаружи почти всегда превращается в
# TerminateProcess (taskkill /F, schtasks End) — это не перехватить; реальный
# перехватываемый путь там SIGBREAK (Ctrl+Break) и CTRL_CLOSE консоли.
STOP = threading.Event()
_stop_signal = None          # имя сигнала, которым остановили (для лога)


def _on_stop_signal(signum, _frame) -> None:
    global _stop_signal
    _stop_signal = signal.Signals(signum).name
    STOP.set()


def install_stop_handlers() -> None:
    """Повесить _on_stop_signal на все сигналы завершения, доступные на
    платформе; отсутствующие (SIGHUP/SIGQUIT на Windows) пропускаются."""
    for name in ("SIGTERM", "SIGHUP", "SIGQUIT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_stop_signal)
        except (OSError, ValueError):    # не главный поток / платформа
            pass


def slide_sel(spec: str) -> int:
    """Селектор тайла 0x66 из имени слайда «источник.метрика»."""
    src, met = spec.split(".", 1)
    return (SLIDE_SOURCES[src] << 4) | SLIDE_METRICS[met]

# XML-регистрация автозадачи без админ-прав (см. install_autostart):
# LogonTrigger ограничен текущим пользователем — обычному пользователю это
# разрешено, в отличие от `schtasks /sc onlogon` (триггер «любой вход»).
AUTOSTART_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Azoth Companion — OLED-демон ROG Azoth 96 HE (замена GearLink): слайдшоу + OSD качельки.</Description>
    <URI>\\Azoth Companion</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%(user)s</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(cmd)s</Command>
      <Arguments>%(args)s</Arguments>
    </Exec>
  </Actions>
</Task>
"""

_FILE_LOG = None      # дубль лога в файл (Logger, создаёт setup_log_file)


def log(msg: str) -> None:
    t = time.time()
    line = (time.strftime("[%H:%M:%S.", time.localtime(t))
            + "%03d] " % (int(t * 1000) % 1000) + msg)
    print(line, flush=True)   # под pythonw stdout=None — print молча пропустит
    if _FILE_LOG is not None:
        _FILE_LOG.info(line)


def setup_log_file(path: str) -> None:
    """Дубль лога в файл: те же строки, что в stdout (не перенаправление!),
    UTF-8, ротация по размеру (стандартный RotatingFileHandler). Пустая строка
    = флаг --log-file без значения: logs/azoth-companion.log рядом с azoth-companion.py
    (каталог создаётся). Относительный PATH считается от CWD."""
    global _FILE_LOG
    if not path:
        path = str(Path(__file__).resolve().parent / "logs" / "azoth-companion.log")
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(p), maxBytes=LOG_MAX_BYTES,
                                      backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError as e:
        print("лог-файл %s недоступен (%s) — пишу только в stdout" % (p, e), flush=True)
        return
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("azoth-companion.file")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(handler)
    _FILE_LOG = lg
    log("лог-файл: %s (ротация %.0f МБ × %d файла)"
        % (p, LOG_MAX_BYTES / 1048576, LOG_BACKUPS + 1))


def pair_label(sel: int, digit: int) -> str:
    hdr = {0: "CPU", 1: "GPU", 2: "VRM", 3: "DRAM", 4: "CHA"}.get((sel >> 4) & 0xF, "?")
    val = {0: "Usage", 1: "Temp", 2: "Freq", 3: "Fan", 4: "Fan", 5: "Volt"}.get(sel & 0xF, "?")
    return "%s%s %s" % (hdr, "" if digit == 0xFF else digit, val)


class HostSensors:
    """Метрики хоста: psutil обязателен по возможности; WMI (LibreHardwareMonitor/
    OpenHardwareMonitor) — для сенсоров temperature/voltage/clock/load CPU/GPU/RAM."""

    LHM_NS = ("root\\LibreHardwareMonitor", "root\\OpenHardwareMonitor")

    def __init__(self):
        try:
            import psutil
            self._p = psutil
            psutil.cpu_percent(interval=None)  # prime: следующий вызов даст дельту
        except ImportError:
            self._p = None
        self._wmi = None
        self._wmi_dead = False    # подсистема LHM в целом (import/namespace)
        self._dead = set()        # «сенсор не найден» — предупреждение один раз на ключ
        self._mm = None           # последний JSON macmon (macOS)
        self._mm_t = 0.0
        self._mm_dead = False     # macmon нет в PATH / не запускается

    def cpu_load(self) -> int | None:
        return None if self._p is None else int(round(self._p.cpu_percent(interval=None)))

    def ram_load(self) -> int | None:
        return None if self._p is None else int(round(self._p.virtual_memory().percent))

    def battery(self):
        if self._p is None or not hasattr(self._p, "sensors_battery"):
            return None
        return self._p.sensors_battery()

    def _lhm_connect(self, what: str):
        """self._wmi или None; подключение ленивое, отказ — одно предупреждение."""
        if sys.platform != "win32":
            self._wmi_dead = True     # LHM/WMI только Windows: на macOS — macmon
            return None
        if self._wmi is not None:
            return self._wmi
        if self._wmi_dead:
            return None
        try:
            import wmi
        except ImportError:
            self._wmi_dead = True
            log("WMI-сенсоры недоступны: pip install wmi + запустить "
                "LibreHardwareMonitor.exe (%s не будет)" % what)
            return None
        for ns in self.LHM_NS:
            try:
                self._wmi = wmi.WMI(namespace=ns)
                return self._wmi
            except Exception:
                continue
        self._wmi_dead = True
        log("WMI-namespace LibreHardwareMonitor не найден — запусти "
            "LibreHardwareMonitor.exe (%s не будет)" % what)
        return None

    def _lhm_pick(self, key: str, stype: str, patterns: tuple[str, ...],
                  what: str, fallback_any: bool = False,
                  scale: float = 1.0) -> int | None:
        """Первый датчик LibreHardwareMonitor типа `stype`, чьё имя (в нижнем
        регистре) содержит хотя бы один из `patterns` (порядок = приоритет
        имён). Подсистема/датчик недоступны → None, предупреждение по одному
        на `key`. fallback_any — если имён из patterns нет совсем, взять
        первый попавшийся датчик типа (некоторые платы зовут Vcore «Voltage #N»)."""
        if key in self._dead:
            return None
        w = self._lhm_connect(what)
        if w is None:
            self._dead.add(key)
            return None
        try:
            rows = w.query("SELECT Name, Value FROM Sensor WHERE SensorType='%s'"
                           % stype)
        except Exception:
            self._dead.add(key)
            log("запрос сенсора «%s» не удался — значение пропускается" % what)
            return None
        vals = [(str(r.Name or "").lower(), float(r.Value))
                for r in rows if r.Value is not None]
        for pat in patterns:
            for name, v in vals:
                if pat in name:
                    return int(round(v * scale))
        if fallback_any and vals:
            return int(round(vals[0][1] * scale))
        self._dead.add(key)
        log("сенсор «%s» не найден (LibreHardwareMonitor: датчик Type='%s' с "
            "именем на «%s») — слайд будет пропускаться" % (what, stype, patterns[0]))
        return None

    def _macmon(self) -> dict | None:
        """macOS: JSON macmon (brew install macmon, sudoless IOReport) —
        температуры CPU/GPU, реальная частота кластеров, gpu-usage. Кэш 1.5 с:
        слайды опрашиваются раз в ~2 с — процесс не спавнится чаще. Нет в
        PATH / не запустился → None (слайды пропустятся штатно) + одно
        предупреждение. На других ОС — None (там LHM/psutil)."""
        if sys.platform != "darwin":
            return None
        if self._mm is not None and time.monotonic() - self._mm_t < 1.5:
            return self._mm
        if self._mm_dead:
            return None
        try:
            r = subprocess.run(["macmon", "pipe", "-i", "200", "-s", "1"],
                               capture_output=True, text=True, timeout=3)
            line = [x for x in r.stdout.splitlines() if x.strip()][-1]
            self._mm = json.loads(line)
        except Exception:
            self._mm_dead = True
            log("macmon недоступен — слайды temp/freq/gpu на macOS пропускаться "
                "будут (brew install macmon)")
            return None
        self._mm_t = time.monotonic()
        return self._mm

    def _macmon_val(self, path: list, scale: float = 1.0) -> int | None:
        """Число из JSON macmon по пути ключей или None."""
        m = self._macmon()
        if m is None:
            return None
        v: object = m
        for k in path:
            if isinstance(v, dict):
                v = v.get(k)
            elif isinstance(v, list) and isinstance(k, int) and k < len(v):
                v = v[k]            # macmon: fans[0].rpm
            else:
                v = None
            if v is None:
                return None
        try:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(round(v * scale))
        except (TypeError, ValueError):
            pass
        return None

    # --- канонические поставщики значений слайдов ---
    def cpu_temp(self) -> int | None:
        """°C пакета CPU: macOS — macmon (cpu_temp_avg); Windows — LHM
        Temperature («CPU Package»); ACPI-термозоны не считаем — на
        тестовой машине их нет (проверено)."""
        t = self._macmon_val(["temp", "cpu_temp_avg"])
        if t is not None:
            return t
        return self._lhm_pick("cpu.temp", "Temperature",
                              ("cpu package", "package", "cpu"),
                              "температура CPU (Package)")

    def cpu_volt(self) -> int | None:
        """Напряжение CPU в милливольтах: LHM Voltage («Vcore»/«CPU»).
        Volt = мВ — подтверждено захватом 12 (PROTOCOL_OLED.md §10.5)."""
        return self._lhm_pick("cpu.volt", "Voltage", ("vcore", "cpu"),
                              "напряжение CPU (Vcore)", fallback_any=True,
                              scale=1000)

    def cpu_freq_mhz(self) -> int:
        """МГц текущей частоты CPU: macOS — macmon (pcpu_freq_mhz, реальные
        кластеры P-ядер); иначе psutil (на AS вернёт базовую). При ошибке
        0 (никогда не None)."""
        v = self._macmon_val(["pcpu_freq_mhz"])
        if v:
            return v
        if self._p is None:                    # psutil нет (macmon тоже не помог)
            return 0
        try:
            f = self._p.cpu_freq()
            return int(round(f.current)) if f and f.current else 0
        except Exception:
            return 0

    def gpu_usage(self) -> int | None:
        """% загрузки GPU: macOS — macmon (gpu_active_ratio); Windows — LHM
        Load («GPU Core», иначе любой GPU-датчик)."""
        u = self._macmon_val(["gpu_active_ratio"], scale=100)
        if u is not None:
            return u
        return self._lhm_pick("gpu.usage", "Load", ("gpu core", "gpu"),
                              "загрузка GPU")

    def gpu_temp(self) -> int | None:
        """°C GPU: macOS — macmon (gpu_temp_avg); Windows — LHM Temperature
        («GPU Core», «Hot Spot», любой GPU)."""
        t = self._macmon_val(["temp", "gpu_temp_avg"])
        if t is not None:
            return t
        return self._lhm_pick("gpu.temp", "Temperature",
                              ("gpu core", "hot spot", "gpu"), "температура GPU")

    def gpu_freq(self) -> int | None:
        """МГц GPU: macOS — macmon (gpu_freq_mhz); Windows — LHM Clock
        («GPU Core»)."""
        f = self._macmon_val(["gpu_freq_mhz"])
        if f is not None:
            return f
        return self._lhm_pick("gpu.freq", "Clock", ("gpu core", "gpu"),
                              "частота GPU")

    def fan_rpm(self) -> int | None:
        """Об/мин основного вентилятора (§10.3.1: тайл Fan, гейдж 1000/3500):
        macOS — macmon (fans[0].rpm); Linux — psutil.sensors_fans; Windows —
        LHM Type='Fan' («CPU», иначе любой). 0 RPM (тихий ход) — валидное
        значение; fanless/нет датчика → None, слайд скипнется."""
        r = self._macmon_val(["fans", 0, "rpm"])
        if r is not None:
            return r
        if sys.platform == "darwin":
            return None             # macmon есть, вентиляторов нет (Air)
        if self._p is not None and hasattr(self._p, "sensors_fans"):
            with suppress(Exception):      # нет датчиков/платформы — скип, не повод логировать
                for fans in self._p.sensors_fans().values():
                    if fans:
                        return int(fans[0].current)
        return self._lhm_pick("cpu.fan", "Fan", ("cpu", "fan"),
                              "об/мин вентилятора CPU", fallback_any=True)

    def gpu_volt(self) -> int | None:
        """мВ GPU: LHM Voltage («GPU Core»), В → мВ."""
        return self._lhm_pick("gpu.volt", "Voltage", ("gpu core", "gpu"),
                              "напряжение GPU", scale=1000)

    def ram_temp(self) -> int | None:
        """°C памяти: LHM Temperature (SODIMM/DIMM/Memory) — платформозависимо."""
        return self._lhm_pick("ram.temp", "Temperature",
                              ("sodimm", "dimm", "memory", "ram"),
                              "температура RAM (DIMM)")

    def ram_freq(self) -> int | None:
        """МГц памяти: LHM Clock («Memory Clock») — платформозависимо."""
        return self._lhm_pick("ram.freq", "Clock",
                              ("memory clock", "memory", "dram"), "частота RAM")

    def ram_volt(self) -> int | None:
        """мВ памяти: LHM Voltage (DIMM/DRAM/VDDCR) — платформозависимо."""
        return self._lhm_pick("ram.volt", "Voltage",
                              ("dimm", "dram", "vddr", "memory"),
                              "напряжение RAM (DIMM)")


def sync_clock(kbd: M901) -> None:
    t = dt.datetime.now()
    ok = kbd.set_clock(t.year, t.month, t.day, t.hour, t.minute)
    log("время %s → 0x63 %s" % (t.strftime("%Y-%m-%d %H:%M"),
                                "ok" if ok else "БЕЗ ОТВЕТА"))


def enabled_slots(args) -> list[int]:
    """Слоты-виджеты по флагам --banner/--clock/--battery/--monitor/--kps;
    без флагов — только баннер (DEFAULT_SLOTS). Пустым набор не бывает:
    OLED требует хотя бы один включённый виджет."""
    slots = [slot for flag, slot in WIDGET_FLAGS if getattr(args, flag)]
    return slots or list(DEFAULT_SLOTS)


def apply_layout(kbd: M901, args) -> None:
    """Стартовая пачка в точности как у GearLink (захват 18):
    65 FF → при необходимости 6A-маска + 68 + 50 55 → пуш значений.
    Набор виджетов = флаги (--banner/--clock/…): включается ровно он,
    слоты вне набора гасятся — демон единственный хозяин раскладки.
    ПОРЯДОК ВАЖЕН: прошивка валидирует каждую 6A против ТЕКУЩЕЙ маски
    («хотя бы один виджет должен остаться»), поэтому включения идут
    раньше выключений — иначе banner-off при наборе без баннера молча
    игнорируется (ACK есть, бит не меняется; проверено 2026-10-05)."""
    kbd.wake_display(0xFF)               # GearLink-style wake (не mode 0!)
    enabled = enabled_slots(args)
    start = args.start if args.start is not None else (
        SLOT_MONITOR if SLOT_MONITOR in enabled else enabled[0])
    mask = kbd.get_status_flags()
    changed = False
    for slot in enabled:                 # сначала включения
        if mask is None or not mask[slot]:
            if not kbd.set_widget(slot, True):           # 6A 00 <slot> 01
                log("внимание: слот %d не подтвердил включение" % slot)
            changed = True
    for slot in range(5):                # …затем выключения лишних
        if slot in enabled or not (mask is None or mask[slot]):
            continue
        if not kbd.set_widget(slot, False):              # 6A 00 <slot> 00
            log("внимание: слот %d не подтвердил выключение" % slot)
        changed = True
    if changed:
        log("маска виджетов: %s → набор %s" % (mask, enabled))
    if args.brightness is not None:
        kbd.set_brightness(args.brightness)          # 68 00 00 00 <v>
        changed = True
    if changed:
        kbd.commit()                    # 50 55 — только после смены маски/яркости
    kbd.select_slot(start)              # 6A 01 <slot>
    kbd.commit()
    log("раскладка: %s; старт=слот %d (%s), маска=%s"
        % ("+".join(WIDGET_NAMES[s] for s in enabled), start,
           WIDGET_NAMES[start], kbd.get_status_flags()))


def demo_value(half: float = 5.5, phase: float = 0.0) -> int:
    """Треугольник 0→100→0, полупериод half с — тестовый «бегущий» сенсор."""
    ph = ((time.monotonic() + phase) % (2 * half)) / half   # 0..2
    return int(round(100 * (ph if ph <= 1.0 else 2.0 - ph)))


def resolve_pairs(args, sensors: HostSensors) -> list[tuple[int, int, int]]:
    """Пары 0x66 по режиму --metrics. Ручные значения (--cpu и т.п.) приоритетны."""
    if args.demo:
        pairs = [(0x00, 0, demo_value())]
        if args.metrics == "cpu-ram" or (args.metrics == "cpu-temp"
                                         and args.temp is None):
            # Второй тайл — RAM как «DRAM0 Usage» (живой тест гипотезы):
            # нейтральная пустая пара выглядит как «CPU Usage 0» на слоте 3.
            pairs.append((0x30, 0, demo_value(half=7.0, phase=2.5)))
        elif args.metrics == "cpu-temp":
            pairs.append((0x01, 0, args.temp))
        return pairs
    cpu = args.cpu if args.cpu is not None else sensors.cpu_load()
    if cpu is None:
        raise SystemExit("нет данных CPU: pip install psutil или задай --cpu")
    if args.metrics == "cpu-temp":
        temp = args.temp if args.temp is not None else sensors.cpu_temp()
        if temp is not None:
            return [(0x00, 0, cpu), (0x01, 0, temp)]
        return [(0x00, 0, cpu)]          # fallback: одинарный Usage (как GearLink)
    if args.metrics == "cpu-ram":
        ram = args.ram_val if args.ram_val is not None else sensors.ram_load()
        if ram is None:
            raise SystemExit("нет данных RAM: pip install psutil или задай --ram-val")
        return [(0x00, 0, cpu), (0x30, 0, ram)]      # «DRAM0 Usage» — гипотеза
    return [(0x00, 0, cpu)]


def resolve_battery(args, sensors: HostSensors) -> int | None:
    if args.bat is not None:
        return args.bat
    bat = sensors.battery()
    if bat is None:
        return None
    return int(round(bat.percent))


def pairs_differ(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return True
    return any(x[0] != y[0] or abs(x[2] - y[2]) >= 1 for x, y in zip(a, b))


def open_events(kbd: M901):
    """Канал событий iface2 (0x93 смена слота, 0x95/0x96 состояние виджета)."""
    try:
        return kbd.open_consumer()
    except Exception as e:
        log("канал событий iface2 недоступен (%s) — продолжаю без него" % e)
        return None


def drain_events(cons) -> None:
    for _ in range(8):
        # timeout_ms=1, не 0: на Windows-hidapi 0 = бесконечное блокирование
        data = cons.read(64, timeout_ms=1)
        if not data:
            return
        if data[0] == 0x03 and len(data) >= 5 and data[1] in (0x93, 0x95, 0x96):
            name = {0x93: "смена слота", 0x95: "статус виджета",
                    0x96: "лок. изменение"}.get(data[1], "?")
            log("событие 0x%02X %s: payload=%s"
                % (data[1], name, bytes(data[4:9]).hex()))


def robust_push(kbd: M901, fn, *args) -> bool:
    """Пуш с самовосстановлением, по режиму отказа:
    • настоящий NAK (`FF AA`) + экран ЯВНО спит (`0x23/02 == 0`) → будим
      `65 FF` + `69` и ретраим (короткий чёрный кадр — только по делу);
    • NAK при включённом экране (OSD качельки/жест монополизировали тракт)
      или таймаут/тишина → тихая ретрия через 0.3 с, без будильника —
      иначе первый burst качельки мигал всю серию (живой тест 2026-10-05)."""
    if fn(*args):
        return True
    if getattr(kbd, "last_nak", False) and not kbd.screen_is_on():
        kbd.wake_display(0xFF)
        kbd.screen_on(True)
        time.sleep(0.2)
        ok = fn(*args)
        if ok:
            log("экран спал — разбудил и повторил")
        return ok
    time.sleep(0.3)
    return fn(*args)


def open_ffc0(kbd: M901):
    """Канал 0xFFC0 (Col03 iface2): зеркало событий 03 93/95/96 + тач 03 71.
    Каждый `03 96` = принятый вертикальный свайп (эмпирика 2026-10-04)."""
    try:
        return kbd.open_ffc0()
    except Exception as e:
        log("канал 0xFFC0 недоступен (%s) — свайпы листать не будут" % e)
        return None


def start_ffc0_reader(kbd, on_tick=None):
    """Фоновый поток: непрерывно читает 0xFFC0 (блокирующее чтение) и кладёт
    фреймы 03-xx в очередь. Спайс-дрен из главного цикла репорты ТЕРЯЛ:
    Windows-HID не копит входные репорты без pending read, а с ним —
    доставляет каждому открытому хендлу. Тики качельки (03 72 01/04)
    дублируются в `on_tick(up)` немедленно — OSD не ждёт главного цикла."""
    try:
        dev = kbd.open_ffc0()
    except Exception as e:
        log("канал 0xFFC0 недоступен (%s) — свайпы листать не будут" % e)
        return None
    q = queue.Queue()

    def reader():
        while True:
            try:
                # ЯВНЫЙ таймаут: read(64) без него = timeout_ms=0 =
                # НЕблокирующее чтение (ловушка cython-hidapi, см. recv)
                data = dev.read(64, timeout_ms=1000)
            except Exception:
                break
            if data:
                d = bytes(data)
                q.put(d)
                # ТОЛЬКО реальные тики объёма (01 = vol+, 04 = vol−). Релиз 00
                # и нажатие 02 окно громкости НЕ открывают: нажатие качельки —
                # это mute/unmute, и после релиза окно с «первым кадром»
                # всплывало с OSD значения на мьюте (регрессия 2026-10-05).
                if on_tick and len(d) >= 3 and d[0] == 0x03 and d[1] == 0x72 \
                        and d[2] in (0x01, 0x04):
                    on_tick(d[2])

    threading.Thread(target=reader, daemon=True).start()
    return q


def poll_ffc0(ffc0, dump: bool = False) -> tuple[int, int]:
    """Выгрести накопленные фреймы из очереди. Возвращает (свайпы 03 96,
    тики качельки 03 72 01/04)."""
    swipes = ticks = 0
    while True:
        try:
            d = ffc0.get_nowait()
        except queue.Empty:
            break
        if dump:
            log("ffc0: %s" % d[:20].hex(" "))
        if d[0] == 0x03 and len(d) >= 3:
            if d[1] == 0x96:
                swipes += 1
            elif d[1] == 0x72 and d[2] in (0x01, 0x04):
                # качелька: 01 = vol+, 04 = vol− (00 = отпускание, не тик)
                ticks += 1
            elif d[1] == 0x94:
                # в захвате 20 GearLink такие видели дважды; если появятся —
                # хотим об этом знать (возможный «сосед» свайпа)
                log("событие 0394: %s" % d[:10].hex(" "))
    return swipes, ticks


def parse_monitor_items(spec: str | None) -> list[str]:
    """--monitor-items «cpu.usage,gpu.temp,…» → проверенный список канонических
    имён. Формат «источник.метрика» — сетка конфига GearLink
    (PROTOCOL_OLED.md §10.5): источники cpu/gpu/ram, метрики usage/temp/freq/
    volt. Короткие имена первой редакции (cpu, ram, temp, …) принимаются как
    алиасы. Неизвестное имя и пустой список — ошибка запуска; дубликаты
    схлопываются, порядок сохраняется; None (флаг не задан) → набор по
    умолчанию."""
    if spec is None:
        return list(DEFAULT_SLIDES)
    names, seen = [], set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        token = SLIDE_ALIASES.get(token, token)
        parts = token.split(".")
        if (len(parts) != 2 or parts[0] not in SLIDE_SOURCES
                or parts[1] not in SLIDE_METRICS):
            raise SystemExit(
                "--monitor-items: неизвестное имя «%s»; формат "
                "«источник.метрика»: источники %s, метрики %s; короткие имена "
                "(%s) тоже принимаются"
                % (raw.strip(), "/".join(SLIDE_SOURCES),
                   "/".join(SLIDE_METRICS), ", ".join(SLIDE_ALIASES)))
        if token not in seen:
            seen.add(token)
            names.append(token)
    if not names:
        raise SystemExit("--monitor-items: пустой список — укажи хотя бы одно "
                         "имя, например cpu.usage,ram.usage,gpu.temp")
    return names


def slide_value(spec: str, args, sensors: HostSensors) -> int | None:
    """Значение слайда «источник.метрика» в единицах тайла 0x66
    (Usage=%, Temp=°C, Freq=МГц, Fan=об/мин, Volt=мВ — PROTOCOL_OLED.md §10.5);
    None = слайд пропускается (сенсор недоступен — предупреждение одно,
    см. HostSensors._lhm_pick). Ручные --cpu/--temp/--ram-val приоритетны.
    cpu.usage/ram.usage без psutil — останов; всё остальное сенсорное —
    LibreHardwareMonitor, «ram» на экране = заголовок «DRAM0»."""
    src, met = spec.split(".", 1)
    if met == "usage":
        if src == "gpu":
            return sensors.gpu_usage()
        if src == "ram":
            v = args.ram_val if args.ram_val is not None else sensors.ram_load()
            if v is None:
                log("нет данных RAM (слайд ram.usage): pip install psutil "
                    "или задай --ram-val")
                raise SystemExit(1)
            return v
        v = args.cpu if args.cpu is not None else sensors.cpu_load()
        if v is None:
            log("нет данных CPU (слайд cpu.usage): pip install psutil "
                "или задай --cpu")
            raise SystemExit(1)
        return v
    if src == "cpu":
        if met == "temp":
            return args.temp if args.temp is not None else sensors.cpu_temp()
        if met == "volt":
            return sensors.cpu_volt()
        if met == "fan":
            return sensors.fan_rpm()
        return sensors.cpu_freq_mhz()                  # cpu.freq
    if src == "gpu":
        return {"temp": sensors.gpu_temp, "freq": sensors.gpu_freq,
                "volt": sensors.gpu_volt}[met]()
    return {"temp": sensors.ram_temp, "freq": sensors.ram_freq,
            "volt": sensors.ram_volt}[met]()           # ram


def _positive_float(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("не число: %r" % text)
    if v <= 0:
        raise argparse.ArgumentTypeError(
            "нужно положительное число, получено %s" % text)
    return v


class WindowsVolume:
    """Мастер-громкость вывода Windows через IAudioEndpointVolume
    (чистый ctypes, без зависимостей). Качелька шлёт consumer-события —
    громкость меняет сама ОС; нам остаётся только ПРОЧИТАТЬ новое
    значение и отзеркалить его на OSD клавиатуры (`51 0C`, как GearLink,
    PROTOCOL_VOLUME.md §0-§1)."""

    def __init__(self):
        self._ep = None
        self._dead = False
        self._cache = None      # значение из фонового поллера
        self._cache_t = 0.0
        self._muted = None      # None = ещё не знаем
        self.on_unmute: Callable[[], None] | None = None   # колбэк «mute снят» (пуш уровня на OSD)
        self._stop = threading.Event()
        self._init_com()

    def _init_com(self):
        import ctypes
        from ctypes import byref, cast, c_float, c_long, c_uint32, c_void_p, POINTER

        self._ct = ctypes
        self._byref, self._cast = byref, cast
        self._c_float, self._c_void_p = c_float, c_void_p
        self._POINTER = POINTER

        class _GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        def guid(s):
            h = s.strip("{}").replace("-", "")
            return _GUID(int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16),
                         (ctypes.c_ubyte * 8)(
                             *[int(h[16 + 2 * i:18 + 2 * i], 16) for i in range(8)]))

        self._CLSID_enum = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        self._IID_enum = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        self._IID_epvol = guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        # прототипы методов COM (индексы vtable см. в вызовах ниже)
        self._P_EP = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_long,
                                        c_long, POINTER(c_void_p))
        self._P_ACT = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(_GUID),
                                         c_uint32, c_void_p, POINTER(c_void_p))
        self._P_F = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(c_float))
        self._P_M = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(ctypes.c_int))
        ctypes.oledll.ole32.CoInitializeEx(None, 0)
        self._GUID_T = _GUID

    def _open(self) -> bool:
        ct, byref, c_void_p = self._ct, self._byref, self._c_void_p
        try:
            enum, dev = c_void_p(), c_void_p()
            ct.oledll.ole32.CoCreateInstance(byref(self._CLSID_enum), None, 1,
                                             byref(self._IID_enum), byref(enum))
            self._call(enum, 4, self._P_EP, 0, 1, byref(dev))   # GetDefaultAudioEndpoint
            ep = c_void_p()
            self._call(dev, 3, self._P_ACT, byref(self._IID_epvol), 1, None,
                       byref(ep))                               # Activate
            self._ep = ep
            return True
        except Exception:
            return False

    def _call(self, obj, idx, proto, *args):
        """Вызов метода COM-объекта по индексу vtable."""
        vt = self._cast(self._cast(obj, self._POINTER(self._c_void_p)).contents,
                        self._POINTER(self._c_void_p))
        fn = self._ct.cast(vt[idx], proto)
        return fn(obj, *args)

    def percent(self) -> int | None:
        """0-100 или None (нет аудиоустройства). Свежий кэш фонового
        поллера отдаётся мгновенно, иначе прямой COM-вызов (~2 мс)."""
        if self._cache is not None and time.monotonic() - self._cache_t < 0.5:
            return self._cache
        if self._dead:
            return None
        for attempt in (1, 2):
            if self._ep is None and not self._open():
                break
            v = self._c_float()
            hr = self._call(self._ep, 9, self._P_F, self._byref(v))
            if hr == 0:
                self._cache = max(0, min(100, int(round(v.value * 100))))
                self._cache_t = time.monotonic()
                return self._cache
            self._ep = None        # устройство могло пересоздаться — откроем заново
        return None

    def fresh(self) -> int | None:
        """Точное значение напрямую из ОС, минуя кэш (для «осадочного» пуша)."""
        if self._dead:
            return None
        for _ in (1, 2):
            if self._ep is None and not self._open():
                break
            v = self._c_float()
            hr = self._call(self._ep, 9, self._P_F, self._byref(v))
            if hr == 0:
                val = max(0, min(100, int(round(v.value * 100))))
                self._cache, self._cache_t = val, time.monotonic()
                return val
            self._ep = None
        return None

    def nudge(self, v: int) -> None:
        """Кэшу придано предсказанное значение (после пуша по тику качельки)."""
        self._cache = max(0, min(100, int(v)))
        self._cache_t = time.monotonic()

    def muted(self) -> bool | None:
        """Состояние mute (GetMute, vtable 15) или None."""
        if self._dead or self._ep is None:
            return None
        m = self._ct.c_int()
        hr = self._call(self._ep, 15, self._P_M, self._byref(m))
        return None if hr != 0 else bool(m.value)

    def start_poller(self, interval: float = 0.1) -> None:
        """Фон: держим громкость «под рукой» (кэш ~10 Гц), чтобы пуш OSD
        не ждал ни COM-вызова, ни главного цикла; попутно ловим переход
        mute→unmute и зовём on_unmute (OSD уровня после Unmute)."""
        self._cache_t = 0.0
        self._cache = None

        def poll():
            while not self._stop.is_set():
                try:
                    self.percent()
                    m = self.muted()
                    if m is not None:
                        if self._muted and not m and self.on_unmute:
                            try:
                                self.on_unmute()
                            except Exception:
                                pass
                        self._muted = m
                except Exception:
                    pass
                self._stop.wait(interval)

        self._stop.clear()
        threading.Thread(target=poll, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class MacVolume:
    """Мастер-громкость вывода macOS через CoreAudio (чистый ctypes, без
    зависимостей). Интерфейс 1:1 с WindowsVolume: percent/fresh/nudge/muted/
    start_poller/stop/on_unmute. Читаем каждое значение напрямую — полный
    цикл (default device + volume + mute) ~0.07 мс, кэш и поллер — как у
    винды. macOS 26 сменила fourcc-селекторы ('defa'→'dOut', volume →
    'volm'), старые возвращают 'who?', поэтому каждый селектор — цепочка
    новых + легаси, рабочий вариант кэшируется первым успехом."""

    _GLOB = 0x676C6F62          # 'glob'
    _OUTP = 0x6F757470          # 'outp'

    def __init__(self):
        self._ca = None
        self._sel = {}          # ключ свойства → рабочий fourcc (1-й успех)
        self._cache = None
        self._cache_t = 0.0
        self._muted_live = None
        self._muted = None      # прошлое состояние — детектор unmute в поллере
        self.on_unmute: Callable[[], None] | None = None
        self._stop = threading.Event()
        try:
            import ctypes
            self._ct = ctypes
            self._ca = ctypes.CDLL(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            self._ca.AudioObjectGetPropertyData.restype = ctypes.c_uint32
            self._ca.AudioObjectGetPropertyData.argtypes = [
                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p]
        except Exception as e:
            log("CoreAudio недоступен (%s) — OSD громкости работать не будет" % e)

    class _AOPA(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                    ("elem", ctypes.c_uint32)]

    def _get(self, key: str, fourccs: tuple[int, ...], oid: int, scope: int,
             typ) -> int | float | None:
        """Свойство объекта по цепочке селекторов (macOS 26+ / легаси).
        Рабочий fourcc кэшируется — дальше один CA-вызов."""
        if self._ca is None:
            return None
        ct = self._ct
        sel = self._sel.get(key)
        for f in ([sel] if sel is not None else fourccs):
            addr = self._AOPA(f, scope, 0)
            v = typ()
            sz = ct.c_uint32(ct.sizeof(typ))
            st = self._ca.AudioObjectGetPropertyData(
                oid, ctypes.byref(addr), 0, None, ctypes.byref(sz),
                ctypes.byref(v))
            if st == 0:
                self._sel[key] = f
                return v.value
        return None

    def _read(self):
        """(vol 0..1, muted) или None. Дефолтное устройство — на каждый запрос:
        при переключении вывода id меняется, lookup копеечный."""
        dev = self._get("dev", (0x644F7574, 0x64656661),   # 'dOut' / 'defa'
                        1, self._GLOB, self._ct.c_uint32)
        if dev is None:
            return None
        vol = self._get("vol", (0x766F6C6D, 0x766F6C75),   # 'volm' / 'volu'
                        int(dev), self._OUTP, self._ct.c_float)
        if vol is None:
            return None
        mute = self._get("mute", (0x6D757465,),            # 'mute'
                         int(dev), self._OUTP, self._ct.c_uint32)
        return vol, bool(mute)

    def fresh(self) -> int | None:
        r = self._read()
        if r is None:
            return None
        self._cache = max(0, min(100, int(round(r[0] * 100))))
        self._cache_t = time.monotonic()
        self._muted_live = r[1]
        return self._cache

    def percent(self) -> int | None:
        if self._cache is not None and time.monotonic() - self._cache_t < 0.5:
            return self._cache
        return self.fresh()

    def nudge(self, v: int) -> None:
        self._cache = max(0, min(100, int(v)))
        self._cache_t = time.monotonic()

    def muted(self) -> bool | None:
        return self._muted_live

    def start_poller(self, interval: float = 0.1) -> None:
        self._cache_t = 0.0
        self._cache = None

        def poll():
            while not self._stop.is_set():
                try:
                    # fresh(), не percent(): кэш громкости живёт 0.5 с, а
                    # _muted_live обновляет только fresh — с percent() переход
                    # mute ловился раз в 0.5+ с (главный лаг unmute-пуша;
                    # полный цикл чтения ~0.07 мс — кэш тут не нужен).
                    self.fresh()
                    m = self.muted()
                    if m is not None:
                        if self._muted and not m and self.on_unmute:
                            try:
                                self.on_unmute()
                            except Exception:
                                pass
                        self._muted = m
                except Exception:
                    pass
                self._stop.wait(interval)

        self._stop.clear()
        threading.Thread(target=poll, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class VolumeWorker(threading.Thread):
    """Стрельба `51 0C` со СТРОГОЙ каденцией в окне серии (live-калибровка
    2026-10-05: интервалы <50 мс ломают OSD-слой — заморозка/блинк/ресеты;
    50 мс (20 Гц) — верхний рабочий темп, каденция ограничивается им).

    Тик качельки открывает окно WINDOW_S (каждый тик продлевает); внутри окна
    воркер раз в 1/hz берёт СВЕЖЕЕ значение из ОС (прямой COM-вызов, ~2 мс) и
    пушит его при изменении; первый такт окна пушит всегда — тик без изменения
    значения (упор в 0/100%) тоже должен показать оверлей. Стрельба таймером,
    а не по событиям: авто-репитов качельки в зеркале нет (один тик на нажатие,
    дамп 2026-10-05), а темп пушей ограничен порогом OSD-слоя, см. выше.
    (Ранний вариант спал фиксированные 20 мс после каждого пуша и во время
    рампа громкости Windows выдавал ~40 Гц — режим гарантированного фриза.)"""

    WINDOW_S = 0.6
    MAX_HZ = 20.0            # live-порог: 50 мс ок, 40 мс — blink/reset

    def __init__(self, kbd: M901, volume: WindowsVolume | MacVolume,
                 hz: float = 12.0):
        super().__init__(daemon=True)
        self._kbd = kbd
        self._vol = volume
        if hz > self.MAX_HZ:
            log("--vol-hz %g превышает порог OSD (%g Гц) — ограничен "
                "(live-калибровка 2026-10-05: <50 мс = фриз)" % (hz, self.MAX_HZ))
            hz = self.MAX_HZ
        self._cadence = 1.0 / max(1.0, float(hz))
        self._until = 0.0
        self._last = None
        self._was_open = False
        self._stop_evt = False

    def tick(self, code: int) -> None:
        self._until = time.monotonic() + self.WINDOW_S

    def stop(self) -> None:
        self._stop_evt = True

    def kick(self) -> None:
        """Открыть окно с ГАРАНТИРОВАННЫМ пушом (реакция на Unmute — запрос
        2026-10-05: «уровень громкости в ответ на Unmute»). _last=None →
        первый такт каденции пушит всегда; NAK (тракт занят сразу после
        нажатия качельки) ретраится на следующем такте — прежний одиночный
        пуш из потока поллера без ретрая терялся («не всегда пушится»).
        Вызывается из потока поллера, воркер делает остальное."""
        self._until = time.monotonic() + self.WINDOW_S
        self._last = None

    def run(self) -> None:
        next_push = 0.0
        while not self._stop_evt:
            now = time.monotonic()
            if now >= self._until:
                self._was_open = False
                time.sleep(0.005)     # короткий idle: старт окна без латентности
                continue
            if now < next_push:       # каденция: не раньше следующего такта
                time.sleep(min(0.005, next_push - now))
                continue
            v = self._vol.fresh()
            first = not self._was_open or self._last is None
            if v is not None and (first or v != self._last):
                if self._kbd.push_volume_osd(v):
                    self._last = v
                    log("OSD громкости: %d%%" % v)
                    # Пока значение меняется при активной серии — продлеваем
                    # окно. Ключево для удержания: vendor-зеркало качельки
                    # авто-репитит один раз на нажатие, а Windows при удержании
                    # сам рампит громкость шагами по 2% — без продления окно
                    # закрылось бы через 0.6 c после последнего тика, и OSD
                    # отстал от рампа (лог 18:35–18:36 2026-10-05).
                    if self._until > now:
                        self._until = now + self.WINDOW_S
                # не прошли — повтор на следующем такте каденции
            self._was_open = True
            next_push = max(next_push + self._cadence, time.monotonic())


def start_gate_watcher(kbd: M901) -> None:
    """Журнал гейта OSD [0x23000CA0] (12 00 payload[8], 10 Гц): 51 0C
    рендерится только при 0 — трассировка поведения гейта в сценарии
    press-hold-release качельки (фриз значения на OLED, 2026-10-05).
    ВАЖНО: 12 00 отвечает только на фиксированном echo=0000."""
    def w():
        prev = None
        while True:
            r = kbd.transact(0x12, 0x00, b"", timeout_ms=150, echo=0)
            if r and r[0] == 0x12 and len(r) > 12:
                g = r[12]
                if g != prev:
                    log("гейт OSD [0x23000CA0] = %d" % g)
                    prev = g
            time.sleep(0.1)

    threading.Thread(target=w, daemon=True).start()


def run(kbd: M901, args) -> None:
    sensors = HostSensors()
    enabled = enabled_slots(args)
    do_monitor = SLOT_MONITOR in enabled
    if args.evt_dump:
        # журнал транзакций: ловим, какой именно обмен NAK'ается в окно блинка
        m901_client.TXLOG = lambda cmd, sub, e, r, nak, ms: log(
            "tx %02X.%02X echo=%04X → %s (%.0f мс)" % (
                cmd, sub, e,
                "NAK" if nak else (r[:8].hex(" ") if r else "нет-ответа"), ms))
        start_gate_watcher(kbd)
    cons = open_events(kbd) if args.events else None
    volume = MacVolume() if sys.platform == "darwin" else WindowsVolume()
    # 50 мс: unmute ловится опросом состояния, квант опроса = задержка пуша
    volume.start_poller(0.05)
    vol_worker = VolumeWorker(kbd, volume, hz=args.vol_hz)
    if not args.no_volume:
        vol_worker.start()
        volume.on_unmute = vol_worker.kick   # Unmute → окно воркера (пуш + ретрай)
    ffc0 = start_ffc0_reader(kbd, on_tick=vol_worker.tick if not args.no_volume else None)
    slides = args.monitor_items   # проверенный непустой список (parse_monitor_items в main)
    n_slides = len(slides)
    slide_warned = set()   # слайды temp/volt, о пропуске которых уже предупредили
    dead_slides = set()    # структурно несуществующие слайды (gpu.fan и пр.)
    last_pairs = None
    last_bat = None
    warn_bat = True
    slide_phase = 0
    slide_tick = -1
    hold_until = 0.0       # пауза автолистания после ручного свайпа
    last_min = None        # минута последней синхронизации часов
    last_push_t = 0.0      # heartbeat: безусловный пуш раз в HEARTBEAT_S
    t_wake = t_stat = time.monotonic()
    if do_monitor:
        if args.slideshow:
            mode_desc = "слайдшоу %.1f с [%s]" % (args.slideshow, ",".join(slides))
            log("слайды: %s" % " → ".join("%s(0x%02X)" % (n, slide_sel(n))
                                          for n in slides))
        else:
            mode_desc = args.metrics
    else:
        mode_desc = "монитор выключен (виджеты: %s)" % ", ".join(
            WIDGET_NAMES[s] for s in enabled)
    log("цикл запущен: интервал %.1f с, %s (Ctrl+C/SIGTERM — выход)"
        % (args.interval, mode_desc))
    if not do_monitor and not args.keep_awake:
        log("динамических виджетов нет — дисплей уснёт по своему таймауту "
            "(пушить нечего, см. --keep-awake)")
    try:
        while not STOP.is_set():
            now = time.monotonic()
            # Дрен 0xFFC0 нужен всегда (очередь не должна расти), тики качельки
            # уходят в VolumeWorker прямо из reader-потока.
            swipes, _ticks = poll_ffc0(ffc0, dump=args.evt_dump) if ffc0 else (0, 0)
            if do_monitor:
                if args.slideshow:
                    # Слайдшоу как у GearLink (§10.6): тайл перезаписывается
                    # одиночным пушем с очередным селектором. Автопрокрутка —
                    # таймером; свайп вниз (03 96) листает немедленно и ставит
                    # автолистание на паузу. Свайп ВВЕРХ прошивка хосту не
                    # сообщает вообще (чистая сессия 2026-10-05: >10 вверх —
                    # ноль событий на обоих каналах iface2), поэтому
                    # «назад» хостом не реализуемо — только вперёд, как GearLink.
                    if swipes:
                        slide_phase = (slide_phase + swipes) % n_slides
                        last_pairs = None
                        hold_until = now + SWIPE_PAUSE_S
                        slide_tick = int(now / args.slideshow)
                        log("свайп вниз → слайд %d/%d «%s» (автолистание на паузе %g с)"
                            % (slide_phase + 1, n_slides, slides[slide_phase],
                               SWIPE_PAUSE_S))
                    elif now >= hold_until:
                        tick = int(now / args.slideshow)
                        if tick != slide_tick:
                            slide_tick = tick
                            slide_phase = (slide_phase + 1) % n_slides
                            last_pairs = None
                    else:
                        # на паузе: держим текущий кадр и фазу таймера
                        slide_tick = int(now / args.slideshow)
                    # Значение очередного слайда; temp/volt без сенсора
                    # пропускаются (однократное предупреждение), автолистание
                    # двигается дальше. Структурно несуществующий слайд
                    # (gpu.fan и т.п.) — одно сообщение в лог, дальше слайд
                    # «мёртв»; мертвы ВСЕ — фолбэк cpu.usage (есть всегда).
                    pairs = None
                    for _ in range(n_slides):
                        name = slides[slide_phase]
                        try:
                            val = slide_value(name, args, sensors)
                        except Exception as e:
                            val = None
                            if name not in dead_slides:
                                dead_slides.add(name)
                                log("слайд «%s»: такого сенсора нет (%s)"
                                    % (name, e))
                                if len(dead_slides) >= n_slides:
                                    log("все слайды без сенсоров — фолбэк cpu.usage")
                                    slides = ["cpu.usage"]
                                    n_slides = 1
                                    slide_phase = 0
                                    slide_tick = int(now / args.slideshow)
                                    slide_warned.clear()
                                    last_pairs = None
                                    break
                            slide_phase = (slide_phase + 1) % n_slides
                            slide_tick = int(now / args.slideshow)
                            continue
                        if val is not None:
                            pairs = [(slide_sel(name), 0, val)]
                            break
                        if name not in slide_warned:
                            slide_warned.add(name)
                            need = ("macmon (brew install macmon)"
                                    if sys.platform == "darwin" else
                                    "запущенный LibreHardwareMonitor (pip install wmi)")
                            log("слайд «%s» пропущен: сенсор недоступен — нужен %s"
                                % (name, need))
                        slide_phase = (slide_phase + 1) % n_slides
                        slide_tick = int(now / args.slideshow)
                else:
                    pairs = resolve_pairs(args, sensors)
                if pairs is not None and (
                        pairs_differ(pairs, last_pairs)
                        or now - last_push_t >= HEARTBEAT_S):
                    if robust_push(kbd, kbd.push_metrics, pairs):
                        log("пуш 0x66: " + ", ".join(
                            "%s=%d" % (pair_label(sel, dig), val)
                            for sel, dig, val in pairs))
                        last_pairs = pairs
                        last_push_t = now
                    else:
                        log("пуш 0x66 не подтверждён (NAK/нет ответа)")
            if args.battery:
                pct = resolve_battery(args, sensors)
                if pct is None:
                    if warn_bat and args.bat is None:
                        log("батарея хоста не найдена — виджет батареи не пушится")
                        warn_bat = False
                elif last_bat is None or abs(pct - last_bat) >= 1:
                    if robust_push(kbd, kbd.set_slot2_value, pct, 0):
                        log("батарея ПК → слот %d: %d%%" % (SLOT_BATTERY, pct))
                        last_bat = pct
            # Экранные часы — статичный текст от 0x63, сами не тикают
            # (подтверждено 2026-10-04: время висело с прошлого запуска).
            # Синхронизируем на границе каждой минуты.
            if args.clock and not args.no_clock:
                now_dt = dt.datetime.now()
                if last_min is None or (now_dt.second < 5 and now_dt.minute != last_min):
                    robust_push(kbd, lambda: sync_clock(kbd))
                    last_min = now_dt.minute
            if args.keep_awake and now - t_wake >= WAKE_EVERY_S:
                kbd.wake_display(0xFF)
                t_wake = now
            if cons:
                drain_events(cons)
            if now - t_stat >= STAT_EVERY_S:
                log("клава: батарея %s%%, слот %s, страница %s"
                    % (kbd.get_battery(), kbd.get_current_slot(),
                       kbd.get_current_page()))
                t_stat = now
            # wait, не sleep: STOP просыпается сразу, не дожидаясь таймаута
            STOP.wait(1.0 if args.slideshow else (0.4 if args.demo else args.interval))
    finally:
        vol_worker.stop()
        volume.stop()
        if cons:
            cons.close()


def once(kbd: M901, args) -> None:
    """Разовая проверка: раскладка + пуш значений, как стартовая пачка GearLink.
    Пушится только включённое (флаги контента включают свой виджет сами);
    после выхода раскладка ОСТАЁТСЯ на экране — это режим визуальной сверки,
    graceful shutdown его не глушит."""
    apply_layout(kbd, args)
    sensors = HostSensors()
    if SLOT_MONITOR in enabled_slots(args):
        pairs = resolve_pairs(args, sensors)
        for i in range(2):
            ok = kbd.push_metrics(pairs)
            log("пуш 0x66 #%d: %s → %s" % (i + 1, ", ".join(
                "%s=%d" % (pair_label(sel, dig), val) for sel, dig, val in pairs),
                "ok" if ok else "БЕЗ ОТВЕТА"))
            if ok and i == 0:
                time.sleep(0.3)
    if args.battery:
        pct = resolve_battery(args, sensors)
        if pct is not None:
            log("батарея ПК %d%% → 0x64 %s" % (pct, kbd.set_slot2_value(pct, 0)))
    if args.clock and not args.no_clock:
        sync_clock(kbd)
    log("готово; экран сейчас — сверяй надписи на OLED")


def status(kbd: M901, args) -> None:
    """Только чтение: ничего не пишет в устройство."""
    print("== устройство ==")
    flags = kbd.get_status_flags()
    if flags is not None:
        on = ["%d %s" % (s, WIDGET_NAMES[s]) for s, f in enumerate(flags) if f]
        print(" маска слотов (0x24/02): %s → включены: %s" % (flags, ", ".join(on) or "нет"))
    print(" текущий слот (0x24/01):", kbd.get_current_slot())
    print(" текущая страница (0x21):", kbd.get_current_page())
    print(" экран включён (0x23/02):", kbd.get_screen_state())
    print(" батарея клавиатуры (0x12/01):", kbd.get_battery(), "%")
    st = kbd.get_status_struct()
    if st:
        print(" последние метрики (0x12/00): temp=%d usage=%d metric3=%d" % (
            int.from_bytes(st[0:2], "little"), int.from_bytes(st[2:4], "little"),
            int.from_bytes(st[4:6], "little")))
    print("== хост ==")
    s = HostSensors()
    print(" CPU %s%%, RAM %s%%, temp %s, батарея %s" % (
        s.cpu_load(), s.ram_load(),
        ("%d°C" % s.cpu_temp()) if s.cpu_temp() is not None else "n/a",
        ("%d%%" % round(s.battery().percent)) if s.battery() else "n/a"))


def wait_reconnect() -> M901 | None:
    """Ждать возврата клавиатуры на шину, вернуть свежий M901
    (или None — останов по сигналу, пока ждали)."""
    warned = False
    while not STOP.is_set():
        try:
            k = M901()
            log("клавиатура вернулась — перезапускаю раскладку")
            return k
        except Exception:
            if not warned:
                log("клавиатуры нет на шине — жду переподключения (Ctrl+C — выход)")
                warned = True
            STOP.wait(2.0)
    return None


def shutdown_widgets(kbd: M901) -> None:
    """Graceful shutdown: погасить все включённые виджеты, КРОМЕ баннера.
    Часы замрут на последней синхронизации, батарея/метрики протухнут —
    без демона динамические виджеты показывают неверные данные; офлайн
    (без пушей с хоста) живут только баннер и KPS, дефолтный остаток —
    баннер. OLED требует хотя бы один включённый виджет (пустая маска
    некорректна), поэтому выключенный баннер включается ПЕРВЫМ и сразу
    становится текущим слотом — и только потом гасится динамика (порядок
    см. в теле). Идемпотентно: гасим только включённые биты маски 1..4;
    ошибки транспорта (устройство уже пропало) не мешают закрытию."""
    try:
        mask = kbd.get_status_flags()
    except Exception as e:
        log("останов: маску слотов не прочитать (%s) — гашу 1..4 вслепую" % e)
        mask = None
    off = [s for s in range(5) if s != SLOT_BANNER and (mask is None or mask[s])]
    # Пустая маска невалидна: баннер выключен (или маску не прочитать) —
    # после гашения динамики возвращаем баннер.
    need_banner = mask is None or not mask[SLOT_BANNER]
    if not off and not need_banner:
        log("останов: динамические виджеты уже выключены (маска %s)" % (mask,))
        return
    # ПОРЯДОК ВАЖЕН (как в apply_layout): прошивка валидирует каждую 6A
    # против текущей маски («ноль виджетов оставить нельзя»), поэтому
    # fallback-баннер включаем и делаем текущим слотом ДО гашения —
    # иначе выключения могут молча игнорироваться (проверено 2026-10-05).
    try:
        if need_banner:
            if not kbd.set_widget(SLOT_BANNER, True):    # 6A 00 00 01
                log("останов: баннер не подтвердил включение")
        kbd.select_slot(SLOT_BANNER)     # 6A 01 00 — баннер становится текущим
    except Exception as e:
        log("останов: баннер не поднять (%s) — гашу динамику дальше" % e)
    for slot in off:
        try:
            if not kbd.set_widget(slot, False):          # 6A 00 <slot> 00
                log("останов: слот %d не подтвердил выключение" % slot)
        except Exception as e:
            log("останов: слот %d не погасился (%s) — транспорт мёртв?" % (slot, e))
            return
    try:
        kbd.commit()                        # 50 55 — маска менялась
    except Exception as e:
        log("останов: commit не прошёл (%s)" % e)
        return
    log("останов: погашены слоты %s, баннер %s"
        % (", ".join("%d %s" % (s, WIDGET_NAMES[s]) for s in off) or "—",
           "включён заново (маска не может быть пустой)" if need_banner
           else "активен"))


# ---------- автозапуск (v0.3): задача планировщика текущего пользователя ----------

def _pythonw() -> str:
    """pythonw.exe того же интерпретатора (запуск БЕЗ консольного окна)."""
    cand = Path(sys.executable).with_name("pythonw.exe")
    if cand.is_file():
        return str(cand)
    return shutil.which("pythonw.exe") or "pythonw.exe"


def autostart_command() -> list[str]:
    """Команда демона в автозадаче: pythonw + этот скрипт + рабочий набор
    флагов. Виджеты заданы явно (без флагов демон включил бы только баннер):
    классический набор часы+батарея+монитор, слайдшоу 2 с. --log-file без
    значения = logs/azoth-companion.log рядом с azoth-companion.py — CWD задачи при логоне
    не гарантирован, поэтому путь должен быть не относительным."""
    return [_pythonw(), str(Path(__file__).resolve()),
            "--clock", "--battery", "--monitor",
            "--slideshow", "2", "--log-file"]


def _schtasks(*argv: str) -> tuple[int, str]:
    r = subprocess.run(["schtasks", *argv], capture_output=True)
    out = b"\n".join(x for x in (r.stdout, r.stderr) if x)
    for enc in ("utf-8", "cp866"):    # schtasks пишет в OEM-кодировке консоли
        try:
            return r.returncode, out.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.returncode, out.decode("utf-8", "replace")


def query_autostart() -> None:
    code, out = _schtasks("/query", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: задача «%s» не найдена" % AUTOSTART_TASK)
        return
    rows = [ln.strip() for ln in out.splitlines() if ln.strip()]
    log("autostart: %s" % (rows[-1] if rows else "задача на месте"))


def install_autostart() -> None:
    """Задача «Azoth Companion»: запуск при логоне текущего пользователя, права
    администратора не нужны. Способ 1 — команда `schtasks /create /sc onlogon`
    (у неё триггер «ЛЮБОЙ вход» — на большинстве машин требует админа);
    при отказе способ 2 — XML-регистрация: LogonTrigger только для текущего
    пользователя + InteractiveToken + LeastPrivilege, т.е. ровно то, что GUI
    планировщика разрешает создавать обычному пользователю.
    ExecutionTimeLimit PT0S — без лимита 72 ч (демон живёт вечно).
    Демон при установке не запускается."""
    if sys.platform != "win32":
        raise SystemExit("автозапуск через schtasks поддержан только на Windows")
    cmd = autostart_command()
    tr = subprocess.list2cmdline(cmd)
    code, out = _schtasks("/create", "/f", "/sc", "onlogon",
                          "/tn", AUTOSTART_TASK, "/tr", tr)
    if code == 0:
        log("autostart: задача «%s» создана: %s" % (AUTOSTART_TASK, tr))
        query_autostart()
        return
    log("autostart: /sc onlogon отклонён (код %d: %s) — регистрирую через XML: "
        "вход только текущего пользователя" % (code, out.strip()))
    import os
    from xml.sax.saxutils import escape
    user = r"%s\%s" % (os.environ.get("USERDOMAIN", "."),
                       os.environ.get("USERNAME", ""))
    xml = AUTOSTART_XML % {"user": escape(user),
                           "cmd": escape(cmd[0]),
                           "args": escape(subprocess.list2cmdline(cmd[1:]))}
    xml_path = Path(__file__).resolve().parent / "logs" / "azoth-companion-task.xml"
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml, encoding="utf-16")   # schtasks ждёт UTF-16
    except OSError as e:
        raise SystemExit("autostart: не записать XML задачи (%s): %s" % (xml_path, e))
    code, out = _schtasks("/create", "/f", "/tn", AUTOSTART_TASK,
                          "/xml", str(xml_path))
    if code != 0:
        log("autostart: НЕ удалось создать задачу «%s» (код %d): %s"
            % (AUTOSTART_TASK, code, out.strip()))
        raise SystemExit(1)
    log("autostart: задача «%s» создана (вход %s): %s"
        % (AUTOSTART_TASK, user, tr))
    query_autostart()


def uninstall_autostart() -> None:
    if sys.platform != "win32":
        raise SystemExit("автозапуск через schtasks поддержан только на Windows")
    code, _ = _schtasks("/query", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: задача «%s» не существует — удалять нечего" % AUTOSTART_TASK)
        return
    code, out = _schtasks("/delete", "/f", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: НЕ удалось удалить задачу (код %d): %s"
            % (code, out.strip()))
        raise SystemExit(1)
    log("autostart: задача «%s» удалена" % AUTOSTART_TASK)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="azoth-companion",
        description="Azoth Companion v0.3 — замена GearLink для ASUS ROG Azoth 96 HE "
                    "(M901): набор виджетов на OLED (баннер/часы/батарея/"
                    "мониторинг/KPS), метрики/слайдшоу и OSD качельки "
                    "громкости. GearLink перед запуском закрыть "
                    "(taskkill //IM GearLink* //F) — иначе NAK-качели. "
                    "При останове (Ctrl+C, SIGTERM) динамические виджеты "
                    "гасятся, баннер остаётся.")

    g = ap.add_argument_group("виджеты (какие слоты включать; без флагов — только баннер)")
    g.add_argument("--banner", action="store_true",
                   help="слот 0: баннер/music mode/кастомный битмап — статичен, "
                        "демону не нужен")
    g.add_argument("--clock", action="store_true",
                   help="слот 1: часы — синхронизация 0x63 на границе каждой минуты")
    g.add_argument("--battery", action="store_true",
                   help="слот 2: батарея ПК — пуш 0x64; --bat включает автоматически")
    g.add_argument("--monitor", action="store_true",
                   help="слот 3: двойной индикатор --metrics / слайдшоу; "
                        "включается автоматически флагами контента "
                        "(--metrics/--slideshow/--monitor-items/--demo/--cpu/"
                        "--temp/--ram-val)")
    g.add_argument("--kps", action="store_true",
                   help="слот 4: нативный KPS-счётчик (клавиш/с) — прошивка "
                        "рисует сама, хост только включает слот")

    g = ap.add_argument_group("рабочий цикл")
    g.add_argument("--interval", type=_positive_float, default=2.0,
                   help="период опроса хоста в обычном режиме, с (по умолчанию 2)")
    g.add_argument("--metrics", choices=("cpu-temp", "cpu-ram", "cpu"),
                   default=None,
                   help="набор тайлов двойного индикатора без слайдшоу: "
                        "usage+temp (по умолчанию; без сенсора — только usage), "
                        "usage+RAM (эксперимент «DRAM0»), только usage; "
                        "включает --monitor")
    g.add_argument("--start", type=int, choices=range(5), default=None,
                   help="какой слот показать после настройки (по умолчанию — "
                        "монитор, если включён, иначе первый включённый; слот "
                        "должен быть в наборе виджетов)")
    g.add_argument("--brightness", type=int, metavar="0-100", default=None,
                   help="переустановить яркость (по умолчанию НЕ трогать)")
    g.add_argument("--keep-awake", action="store_true",
                   help="будить OLED `65 FF` раз в 60 с")
    g.add_argument("--no-clock", action="store_true",
                   help="не синхронизировать время")

    g = ap.add_argument_group("слайдшоу (одиночные тайлы 0x66 с echo=0, как GearLink)")
    g.add_argument("--slideshow", type=_positive_float, metavar="SEC", default=None,
                   help="листать слайды каждые SEC секунд (набор — "
                        "--monitor-items, по умолчанию cpu.usage,ram.usage,"
                        "cpu.freq); свайп вниз листает вручную и ставит "
                        "автолистание на паузу 5 с; включает --monitor")
    g.add_argument("--monitor-items", default=None, metavar="ИСТ.МЕТРИКА[,…]",
                   help="слайды через запятую, формат «источник.метрика» — "
                        "сетка конфига GearLink: источники cpu/gpu/ram, "
                        "метрики usage/temp/freq/volt/fan, например cpu.usage,"
                        "cpu.freq,ram.usage,gpu.temp; ram рисуется заголовком "
                        "«DRAM0» (0x30), gpu — «GPU0» (0x10); сенсорные слайды "
                        "(все temp/freq/volt и gpu.usage) требуют запущенный "
                        "LibreHardwareMonitor — без сенсора слайд пропускается "
                        "с предупреждением; короткие имена (cpu, ram, freq, "
                        "temp, volt) тоже принимаются; --monitor-items без "
                        "--slideshow включает слайдшоу с периодом 2 с "
                        "(бывш. --slides)")

    g = ap.add_argument_group("ручные значения (тесты без сенсоров)")
    g.add_argument("--cpu", type=int, metavar="0-100", default=None,
                   help="ручное значение CPU %% (слайд cpu.usage и --metrics; "
                        "включает --monitor)")
    g.add_argument("--temp", type=int, default=None,
                   help="ручная температура °C (слайд cpu.temp и --metrics "
                        "cpu-temp; включает --monitor)")
    g.add_argument("--ram-val", type=int, metavar="0-100", default=None,
                   help="ручное значение RAM %% (слайд ram.usage и --metrics "
                        "cpu-ram; включает --monitor)")
    g.add_argument("--bat", type=int, metavar="0-100", default=None,
                   help="ручное значение батареи ПК %% (включает --battery)")

    g = ap.add_argument_group("громкость (OSD качельки)")
    g.add_argument("--vol-hz", type=_positive_float, default=12.0, metavar="HZ",
                   help="частота пушей OSD внутри окна серии качельки, Гц "
                        "(live-калибровка 2026-10-05: OSD непрерывно обновляется "
                        "при интервале >=50 мс; 40 мс и ниже — фриз/блинк, "
                        "поэтому значения выше 20 Гц ограничиваются; дефолт 12 — "
                        "с запасом)")
    g.add_argument("--no-volume", action="store_true",
                   help="не слушать качельку и не пушить OSD громкости "
                        "(например, если аудиоустройство недоступно)")

    g = ap.add_argument_group("разовые режимы и диагностика")
    g.add_argument("--once", action="store_true",
                   help="настроить и вытолкнуть значения один раз, без цикла")
    g.add_argument("--status", action="store_true",
                   help="только показать статус устройства и хоста (read-only)")
    g.add_argument("--demo", action="store_true",
                   help="тест сенсоров: CPU-тайл бегает 0→100→0 (~5.5 с в сторону), "
                        "интервал пуша 0.4 с; включает --monitor")
    g.add_argument("--events", action="store_true",
                   help="слушать iface2 (смена слота/статус виджета) и писать в лог")
    g.add_argument("--evt-dump", action="store_true",
                   help="дампить все фреймы 0xFFC0 (03 71/93/95/96) и журнал "
                        "транзакций (TXLOG) в лог")

    g = ap.add_argument_group("лог-файл и автозапуск")
    g.add_argument("--log-file", nargs="?", const="", default=None, metavar="PATH",
                   help="дублировать лог в файл, UTF-8, ротация ~2 МБ × 3 файла; "
                        "без PATH — logs/azoth-companion.log рядом с azoth-companion.py")
    g.add_argument("--install-autostart", action="store_true",
                   help="создать задачу планировщика «Azoth Companion» (запуск при логоне "
                        "текущего пользователя через pythonw — без консольного "
                        "окна; флаги демона: --clock --battery --monitor "
                        "--slideshow 2 --log-file); права администратора не "
                        "нужны; демон сейчас не запускается")
    g.add_argument("--uninstall-autostart", action="store_true",
                   help="удалить задачу планировщика «Azoth Companion»")
    args = ap.parse_args()

    if args.log_file is not None:        # "" = --log-file без значения → путь по умолчанию
        setup_log_file(args.log_file)
    if args.install_autostart:
        install_autostart()
        return
    if args.uninstall_autostart:
        uninstall_autostart()
        return

    # Флаги контента включают свой виджет сами (как --slides раньше включал
    # слайдшоу): метрики/слайды/демо → монитор, --bat → батарея.
    if (args.metrics is not None or args.slideshow is not None
            or args.monitor_items is not None or args.demo
            or args.cpu is not None or args.temp is not None
            or args.ram_val is not None):
        args.monitor = True
    if args.bat is not None:
        args.battery = True
    if args.metrics is None:
        args.metrics = "cpu-temp"        # умолчание --metrics (уже после импликации)
    items_given = args.monitor_items is not None   # parse_monitor_items(None)
    args.monitor_items = parse_monitor_items(args.monitor_items)  # вернёт умолчание
    if items_given and args.slideshow is None:
        args.slideshow = DEFAULT_SLIDESHOW_S   # --monitor-items включает слайдшоу
    if args.start is not None and args.start not in enabled_slots(args):
        raise SystemExit("--start %d: слот не включён (набор: %s) — добавь "
                         "соответствующий флаг виджета (--banner/--clock/"
                         "--battery/--monitor/--kps)"
                         % (args.start, ", ".join(
                             "%d %s" % (s, WIDGET_NAMES[s])
                             for s in enabled_slots(args))))

    install_stop_handlers()
    try:
        kbd = M901()
    except Exception as e:
        if args.status or args.once:
            raise SystemExit("клавиатура не найдена: %s\n"
                             "(USB подключён? GearLink закрыт? pip install hidapi)" % e)
        log("клавиатура не найдена (%s)" % e)
        kbd = wait_reconnect()
        if kbd is None:                  # останов по сигналу, пока ждали
            return
    log("подключено: %s" % kbd.dev.get_product_string())
    daemon = not (args.status or args.once)
    try:
        if args.status:
            status(kbd, args)
        elif args.once:
            once(kbd, args)
        else:
            try:
                while not STOP.is_set():
                    apply_layout(kbd, args)
                    try:
                        run(kbd, args)
                        break
                    except OSError as e:
                        # USB-линк умер (перечисление, глюк драйвера, выдёргивание)
                        log("устройство пропало (%s)" % e)
                        try:
                            kbd.close()
                        except Exception:
                            pass
                        kbd = wait_reconnect()
                        if kbd is None:  # останов по сигналу, пока ждали
                            break
            except KeyboardInterrupt:    # Ctrl+C: SIGINT не перекрыт обработчиком
                log("остановлено пользователем (Ctrl+C)")
    finally:
        if daemon:
            if _stop_signal:
                log("останов по сигналу %s" % _stop_signal)
            try:
                shutdown_widgets(kbd)    # погасить всё, кроме баннера
            except Exception:
                pass
        try:
            kbd.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
