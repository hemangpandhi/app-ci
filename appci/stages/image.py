"""Build the emulator system image (with coverage instrumentation for the configured apps)."""
from __future__ import annotations

from ..model import FAILED, PASSED
from . import Stage, register
from .build import build_error_lines


@register
class ImageStage(Stage):
    name = "image"
    title = "Emulator image (full build)"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("image")
        target = scfg["target"]
        d = ctx.stage_dir("image", clean=True)
        log = d / "image.log"
        res.log = ctx.rel_report(log)
        with ctx.tree.lock(target):
            # `m` with no goal builds droid: system/vendor images the emulator boots from.
            cmd = ctx.tree.shell(target, "m", log, timeout=scfg.get("timeout"))
        if not cmd.ok:
            errs = build_error_lines(cmd.output())
            res.status, res.summary = FAILED, (errs[0][:200] if errs else f"m exited {cmd.returncode}")
            return
        res.status = PASSED
        res.summary = f"image ready in {ctx.tree.product_out(target)}"
