"""Headless AAOS emulator lifecycle, booted from the image built for the emulator lunch target."""
from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from . import util
from .aosp import AospTree

IMAGE_MARKERS = ("system-qemu.img", "system.img")


def image_ready(tree: AospTree, target: str) -> bool:
    po = tree.product_out(target)
    return any((po / m).is_file() for m in IMAGE_MARKERS)


def adb(serial: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "-s", serial, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)


def wait_boot(serial: str, timeout: float) -> None:
    deadline = time.time() + timeout
    subprocess.run(["adb", "-s", serial, "wait-for-device"], timeout=timeout)
    while time.time() < deadline:
        try:
            if adb(serial, "shell", "getprop", "sys.boot_completed", timeout=20).stdout.strip() == "1":
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    raise TimeoutError(f"{serial} did not finish booting within {timeout}s")


def prepare_device(serial: str) -> None:
    """Make UI tests deterministic: root, no animations, screen on and unlocked."""
    adb(serial, "root", timeout=60)
    time.sleep(3)
    subprocess.run(["adb", "-s", serial, "wait-for-device"], timeout=120)
    for setting in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
        adb(serial, "shell", "settings", "put", "global", setting, "0")
    adb(serial, "shell", "svc", "power", "stayon", "true")
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb(serial, "shell", "wm", "dismiss-keyguard")


class Emulator:
    def __init__(self, tree: AospTree, target: str, port: int, args: List[str], boot_timeout: int, log: Path,
                 sysdir: Optional[Path] = None, binary: Optional[Path] = None):
        self.tree, self.target, self.port = tree, target, port
        self.sysdir, self.binary = sysdir, binary
        self.args, self.boot_timeout, self.log = args, boot_timeout, log
        self.serial = f"emulator-{port}"
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> str:
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise RuntimeError("/dev/kvm is not accessible; add the runner user to the 'kvm' group")
        if self.sysdir is None and not image_ready(self.tree, self.target):
            raise RuntimeError(f"no emulator image in {self.tree.product_out(self.target)}; run `appci image` first")
        # Kill a stale instance left on the same port by a crashed run.
        with contextlib.suppress(Exception):
            adb(self.serial, "emu", "kill", timeout=10)
        if self.sysdir is not None:
            # Downloaded image: the emulator's build mode (ANDROID_PRODUCT_OUT) on the unpacked
            # release; writable images (userdata-qemu.img, ...) go next to it, wiped every boot.
            binary = self.binary or (self.tree.root / "prebuilts/android-emulator/linux-x86_64/emulator")
            if not binary.is_file():
                raise RuntimeError(f"emulator program {binary} missing (configure emulator.program.url)")
            env = util.merge_env({"ANDROID_PRODUCT_OUT": str(self.sysdir), "ANDROID_BUILD_TOP": str(self.tree.root),
                                  "ANDROID_EMULATOR_HOME": str(self.log.parent / "emulator-home")})
            script = (f"exec {shlex.quote(str(binary))} -sysdir {shlex.quote(str(self.sysdir))} "
                      f"-port {self.port} {' '.join(shlex.quote(a) for a in self.args)}")
        else:
            env = util.merge_env(self.tree.env(self.target))
            script = (
                f"cd {shlex.quote(str(self.tree.root))} && source build/envsetup.sh >/dev/null && "
                f"lunch {shlex.quote(self.tree.lunch(self.target))} >/dev/null && "
                f"exec emulator -port {self.port} {' '.join(shlex.quote(a) for a in self.args)}"
            )
        self.log.parent.mkdir(parents=True, exist_ok=True)
        lf = open(self.log, "wb")
        util.info(f"booting emulator {self.serial} ({self.sysdir or self.tree.lunch(self.target)}) ...")
        self.proc = subprocess.Popen(["bash", "-c", script], stdout=lf, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
        try:
            wait_boot(self.serial, self.boot_timeout)
        except Exception:
            self.stop()
            raise
        prepare_device(self.serial)
        util.ok(f"emulator {self.serial} booted")
        return self.serial

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            adb(self.serial, "emu", "kill", timeout=20)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
