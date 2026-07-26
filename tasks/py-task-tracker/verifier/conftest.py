"""task-tracker verifier：每个测试在独立临时目录运行 `python -m tracker`。"""
import subprocess
import sys
from pathlib import Path

import pytest

WORK_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    """独立数据库文件 + 以 work 根为模块搜索路径。"""
    db = tmp_path / "tasks.json"
    return {"dir": tmp_path, "db": db}


def run_cli(env, *args, cwd=None):
    import os
    e = dict(os.environ)
    e["TRACKER_DB"] = str(env["db"])
    e["PYTHONPATH"] = str(WORK_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "tracker", *args],
        capture_output=True, text=True, timeout=30,
        cwd=str(cwd or env["dir"]), env=e,
    )
