"""SPEC 覆盖：正常路径 / 边界 / 错误路径 / 持久化 / 导入幂等与往返。"""
import json

from conftest import run_cli


def add(env, title, *extra):
    p = run_cli(env, "add", title, *extra)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


# ---------- add ----------

def test_add_prints_incrementing_ids(env_dir):
    assert add(env_dir, "任务一") == "1"
    assert add(env_dir, "任务二") == "2"


def test_add_with_due_and_priority(env_dir):
    add(env_dir, "t", "--due", "2026-08-01", "--priority", "high")
    p = run_cli(env_dir, "list")
    line = p.stdout.strip()
    fields = line.split("\t")
    assert fields[0] == "1" and fields[1] == "open"
    assert fields[2] == "high" and fields[3] == "2026-08-01" and fields[4] == "t"


def test_add_invalid_date_exit_2_and_no_write(env_dir):
    p = run_cli(env_dir, "add", "t", "--due", "2026-02-30")
    assert p.returncode == 2
    assert p.stderr.strip()
    assert run_cli(env_dir, "list").stdout == ""


def test_add_malformed_date_exit_2(env_dir):
    p = run_cli(env_dir, "add", "t", "--due", "08/01/2026")
    assert p.returncode == 2


def test_add_invalid_priority_exit_2(env_dir):
    p = run_cli(env_dir, "add", "t", "--priority", "urgent")
    assert p.returncode == 2
    assert run_cli(env_dir, "list").stdout == ""


# ---------- list ----------

def test_list_empty_no_output_exit_0(env_dir):
    p = run_cli(env_dir, "list")
    assert p.returncode == 0
    assert p.stdout == ""


def test_list_format_and_null_due_dash(env_dir):
    add(env_dir, "无截止")
    p = run_cli(env_dir, "list")
    assert p.stdout.strip() == "1\topen\tmedium\t-\t无截止"


def test_list_status_filter(env_dir):
    add(env_dir, "a")
    add(env_dir, "b")
    run_cli(env_dir, "done", "1")
    open_out = run_cli(env_dir, "list", "--status", "open").stdout
    done_out = run_cli(env_dir, "list", "--status", "done").stdout
    assert "b" in open_out and "a" not in open_out
    assert "a" in done_out and "b" not in done_out


def test_list_due_before_strict_boundary(env_dir):
    add(env_dir, "早", "--due", "2026-08-01")
    add(env_dir, "边界", "--due", "2026-08-05")
    add(env_dir, "晚", "--due", "2026-08-09")
    add(env_dir, "无期限")
    out = run_cli(env_dir, "list", "--due-before", "2026-08-05").stdout
    assert "早" in out
    assert "边界" not in out          # 严格早于
    assert "晚" not in out and "无期限" not in out


# ---------- done ----------

def test_done_marks_and_is_idempotent(env_dir):
    add(env_dir, "a")
    assert run_cli(env_dir, "done", "1").returncode == 0
    assert run_cli(env_dir, "done", "1").returncode == 0
    assert "done" in run_cli(env_dir, "list").stdout.split("\t")[1]


def test_done_unknown_id_exit_3(env_dir):
    p = run_cli(env_dir, "done", "99")
    assert p.returncode == 3
    assert p.stderr.strip()


# ---------- 持久化（跨进程） ----------

def test_persistence_across_processes(env_dir):
    add(env_dir, "a", "--due", "2026-08-01")
    run_cli(env_dir, "done", "1")
    # 每次 run_cli 都是新进程；状态必须来自磁盘
    out = run_cli(env_dir, "list", "--status", "done").stdout
    assert "a" in out and "2026-08-01" in out


# ---------- 损坏文件恢复 ----------

def test_corrupt_db_exit_2_no_traceback_no_overwrite(env_dir):
    env_dir["db"].write_text("{not json!", encoding="utf-8")
    p = run_cli(env_dir, "list")
    assert p.returncode == 2
    assert "corrupt" in p.stderr.lower()
    assert "Traceback" not in p.stderr
    assert env_dir["db"].read_text(encoding="utf-8") == "{not json!"  # 未被覆盖


# ---------- export / import ----------

def test_export_format(env_dir):
    add(env_dir, "a", "--due", "2026-08-01")
    out_file = env_dir["dir"] / "out.json"
    p = run_cli(env_dir, "export", str(out_file))
    assert p.returncode == 0
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["tasks"][0]["title"] == "a" and data["tasks"][0]["id"] == 1


def test_export_import_roundtrip_to_fresh_db(env_dir, tmp_path):
    add(env_dir, "a", "--due", "2026-08-01", "--priority", "high")
    add(env_dir, "b")
    run_cli(env_dir, "done", "2")
    out_file = env_dir["dir"] / "out.json"
    run_cli(env_dir, "export", str(out_file))

    fresh = {"dir": env_dir["dir"], "db": tmp_path / "fresh.json"}
    p = run_cli(fresh, "import", str(out_file))
    assert p.returncode == 0
    assert p.stdout.strip() == "imported 2"
    out = run_cli(fresh, "list").stdout
    assert "a" in out and "b" in out and "high" in out
    assert "done" in run_cli(fresh, "list", "--status", "done").stdout


def test_import_idempotent(env_dir):
    add(env_dir, "a")
    out_file = env_dir["dir"] / "out.json"
    run_cli(env_dir, "export", str(out_file))
    p1 = run_cli(env_dir, "import", str(out_file))
    assert p1.stdout.strip() == "imported 0"   # 全部已存在
    p2 = run_cli(env_dir, "import", str(out_file))
    assert p2.stdout.strip() == "imported 0"
    lines = [ln for ln in run_cli(env_dir, "list").stdout.splitlines() if ln]
    assert len(lines) == 1                     # 未重复


def test_import_preserves_original_ids_and_skips_existing(env_dir, tmp_path):
    src = {"dir": env_dir["dir"], "db": tmp_path / "src.json"}
    add(src, "one")            # id 1
    add(src, "two")            # id 2
    out_file = env_dir["dir"] / "out.json"
    run_cli(src, "export", str(out_file))

    add(env_dir, "local-one")  # 目标库已有 id 1
    p = run_cli(env_dir, "import", str(out_file))
    assert p.returncode == 0
    assert p.stdout.strip() == "imported 1"    # 只新增 id 2
    out = run_cli(env_dir, "list").stdout
    assert "local-one" in out and "two" in out
    assert "\tone" not in out                  # id 1 未被覆盖


def test_import_missing_file_exit_2(env_dir):
    p = run_cli(env_dir, "import", str(env_dir["dir"] / "nope.json"))
    assert p.returncode == 2
    assert run_cli(env_dir, "list").stdout == ""


def test_import_invalid_format_exit_2_state_unchanged(env_dir):
    add(env_dir, "a")
    bad = env_dir["dir"] / "bad.json"
    bad.write_text("[1,2,3]", encoding="utf-8")
    p = run_cli(env_dir, "import", str(bad))
    assert p.returncode == 2
    lines = [ln for ln in run_cli(env_dir, "list").stdout.splitlines() if ln]
    assert len(lines) == 1


# ---------- 未知子命令 ----------

def test_unknown_command_exit_2(env_dir):
    p = run_cli(env_dir, "frobnicate")
    assert p.returncode == 2
