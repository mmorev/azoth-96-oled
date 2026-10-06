"""Autostart (v0.3): a current-user scheduler task — an autonomous script
with its own `install|uninstall` mini-CLI (D6; the autostart flags left the
daemon CLI — the BREAKING change of the restructure). Run:
    python scripts/install.py install
    python scripts/install.py uninstall"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

try:
    from azoth.log import log
except ImportError:                   # run as a script: python scripts/install.py …
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from azoth.log import log

AUTOSTART_TASK = "Azoth Companion"                # the scheduler task name (the current user)

# Registering the autostart task via XML without admin rights (see install_autostart):
# the LogonTrigger is limited to the current user — a regular user is allowed
# this, unlike `schtasks /sc onlogon` (the "any logon" trigger).
AUTOSTART_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Azoth Companion — the OLED daemon for the ROG Azoth 96 HE (a GearLink replacement): slideshow + volume rocker OSD.</Description>
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


def _pythonw() -> str:
    """The pythonw.exe of the same interpreter (running WITHOUT a console window)."""
    cand = Path(sys.executable).with_name("pythonw.exe")
    if cand.is_file():
        return str(cand)
    return shutil.which("pythonw.exe") or "pythonw.exe"


def autostart_command() -> list[str]:
    """The daemon command in the autostart task: pythonw + main.py + the
    working flag set. The widgets are given explicitly (without flags the
    daemon would enable the banner only): the classic clock+battery+monitor
    set, a 2 s slideshow. --log-file without a value = logs/azoth-companion.log
    next to main.py — the task CWD at logon is not guaranteed, so
    the path must not be relative."""
    return [_pythonw(), str(Path(__file__).resolve().parent.parent / "main.py"),
            "--clock", "--battery", "--monitor",
            "--slideshow", "2", "--log-file"]


def _schtasks(*argv: str) -> tuple[int, str]:
    r = subprocess.run(["schtasks", *argv], capture_output=True)
    out = b"\n".join(x for x in (r.stdout, r.stderr) if x)
    for enc in ("utf-8", "cp866"):    # schtasks writes in the console OEM encoding
        try:
            return r.returncode, out.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.returncode, out.decode("utf-8", "replace")


def query_autostart() -> None:
    code, out = _schtasks("/query", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: the task \"%s\" not found" % AUTOSTART_TASK)
        return
    rows = [ln.strip() for ln in out.splitlines() if ln.strip()]
    log("autostart: %s" % (rows[-1] if rows else "the task is in place"))


def install_autostart() -> None:
    """The "Azoth Companion" task: starts at the current user's logon, no
    administrator rights needed. Method 1 — the `schtasks /create /sc
    onlogon` command (its trigger is "ANY logon" — on most machines it
    requires admin); on refusal, method 2 — an XML registration: a
    LogonTrigger for the current user only + InteractiveToken +
    LeastPrivilege, i.e. exactly what the scheduler GUI lets a regular user
    create. ExecutionTimeLimit PT0S — no 72 h limit (the daemon lives
    forever). The daemon is not started at install time."""
    if sys.platform != "win32":
        raise SystemExit("autostart via schtasks is supported on Windows only")
    cmd = autostart_command()
    tr = subprocess.list2cmdline(cmd)
    code, out = _schtasks("/create", "/f", "/sc", "onlogon",
                          "/tn", AUTOSTART_TASK, "/tr", tr)
    if code == 0:
        log("autostart: the task \"%s\" created: %s" % (AUTOSTART_TASK, tr))
        query_autostart()
        return
    log("autostart: /sc onlogon rejected (code %d: %s) — registering via XML: "
        "the current user's logon only" % (code, out.strip()))
    import os
    from xml.sax.saxutils import escape
    user = r"%s\%s" % (os.environ.get("USERDOMAIN", "."),
                       os.environ.get("USERNAME", ""))
    xml = AUTOSTART_XML % {"user": escape(user),
                           "cmd": escape(cmd[0]),
                           "args": escape(subprocess.list2cmdline(cmd[1:]))}
    xml_path = Path(__file__).resolve().parent.parent / "logs" / "azoth-companion-task.xml"
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml, encoding="utf-16")   # schtasks expects UTF-16
    except OSError as e:
        raise SystemExit("autostart: cannot write the task XML (%s): %s" % (xml_path, e))
    code, out = _schtasks("/create", "/f", "/tn", AUTOSTART_TASK,
                          "/xml", str(xml_path))
    if code != 0:
        log("autostart: FAILED to create the task \"%s\" (code %d): %s"
            % (AUTOSTART_TASK, code, out.strip()))
        raise SystemExit(1)
    log("autostart: the task \"%s\" created (the logon of %s): %s"
        % (AUTOSTART_TASK, user, tr))
    query_autostart()


def uninstall_autostart() -> None:
    if sys.platform != "win32":
        raise SystemExit("autostart via schtasks is supported on Windows only")
    code, _ = _schtasks("/query", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: the task \"%s\" does not exist — nothing to remove" % AUTOSTART_TASK)
        return
    code, out = _schtasks("/delete", "/f", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: FAILED to remove the task (code %d): %s"
            % (code, out.strip()))
        raise SystemExit(1)
    log("autostart: the task \"%s\" removed" % AUTOSTART_TASK)


def main() -> None:
    """The mini-CLI: `install` registers the logon task, `uninstall` removes it."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "install":
        install_autostart()
    elif cmd == "uninstall":
        uninstall_autostart()
    else:
        raise SystemExit("usage: python scripts/install.py install|uninstall")


if __name__ == "__main__":
    main()
