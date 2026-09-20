from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_PREAMBLE = "from hotaru.response import ModuleContext\nctx: ModuleContext\n"


class TypeCheckError(RuntimeError):
    pass


def _run(paths: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "basedpyright", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr or "pyright strict failed").strip()
        raise TypeCheckError(detail)


def check_kernel() -> None:
    _run(["hotaru", "relay"])


def check_hmod(source: str, path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hotaru-pyright-") as tmp:
        dest = Path(tmp) / f"{path.stem}.py"
        dest.write_text(_PREAMBLE + source, encoding="utf-8")
        _run([str(dest)])
