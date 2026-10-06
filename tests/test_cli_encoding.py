"""Every argparse CLI must print --help as ASCII under legacy Windows code pages."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
CLIS = sorted(
    p for p in [*ROOT.glob("skills/*/scripts/*.py"), *ROOT.glob("tools/*.py")]
    if "argparse" in p.read_text(encoding="utf-8", errors="replace")
    and "__main__" in p.read_text(encoding="utf-8", errors="replace")
)


def test_cli_inventory_nonempty():
    assert len(CLIS) > 20


@pytest.mark.parametrize("encoding", ["cp1252", "cp950"])
@pytest.mark.parametrize("script", CLIS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_help_is_ascii(script, encoding, tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONUTF8": "0", "MPLBACKEND": "Agg"}
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path, env=env, capture_output=True, timeout=120,
    )
    stderr = result.stderr.decode("ascii", "backslashreplace")
    assert result.returncode == 0, f"{script.name} --help exited {result.returncode}:\n{stderr[-2000:]}"
    try:
        result.stdout.decode("ascii")
    except UnicodeDecodeError as exc:
        bad = result.stdout[max(0, exc.start - 40):exc.end + 40]
        pytest.fail(f"{script.name} --help is not ASCII under {encoding}: {bad!r}")
