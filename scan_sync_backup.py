#!/usr/bin/env python3
"""全量备份到硬盘，保存硬盘基准，并把后续增量上传到云端。"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

try:
    import sqlite3
    if sqlite3.sqlite_version_info < (3, 22, 0):
        # 旧版SQLite缺少完善的WAL只读支持，CentOS使用已声明的备用驱动。
        import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # 部分自行编译的CentOS Python缺少内置_sqlite3。
    import pysqlite3 as sqlite3


# ---------------------------------------------------------------------------
# 用户配置

# 备份目录
SOURCE_PATH = "/files"
# 备份的磁盘路径
DISK_BACKUP_PATH = "/mnt/files"
# 备份状态的SQLite数据库路径
DATABASE_PATH = os.environ.get("VIEW_DATABASE_PATH", "./data/backup_state.db")

# 云服务器的配置
CLOUD_HOST = "10.90.0.4"
CLOUD_USER = "root"
CLOUD_PATH = "/data/files"
CLOUD_SSH_PORT = 22
CLOUD_SSH_KEY = ""
# 密码通过SSHPASS环境变量传递，不写入命令行和SQLite。
CLOUD_PASSWORD = ""
CLOUD_BWLIMIT = 0

BATCH_SIZE = 5000
PROGRESS_EVERY = 10000
MTIME_TOLERANCE_NS = 1_000_000_000
RSYNC_COMMAND = "rsync"
SSH_COMMAND = "ssh"
SSHPASS_COMMAND = "sshpass"
RSYNC_PARTIAL_DIR = ".rsync-partial"

SCRIPT_DIR = Path(__file__).resolve().parent


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    entry_count INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    rsync_exit_code INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS baseline_entries (
    relative_path TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    mode INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    link_target TEXT,
    xattr_hash TEXT NOT NULL,
    backed_up_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_entries (
    relative_path TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    mode INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    link_target TEXT,
    xattr_hash TEXT NOT NULL,
    source_exists INTEGER NOT NULL DEFAULT 1,
    scan_run_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    cloud_entry_type TEXT,
    cloud_size INTEGER,
    cloud_mtime_ns INTEGER,
    cloud_mode INTEGER,
    cloud_uid INTEGER,
    cloud_gid INTEGER,
    cloud_link_target TEXT,
    cloud_xattr_hash TEXT,
    cloud_uploaded_at TEXT,
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    last_upload_at TEXT,
    last_upload_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_exists ON source_entries(source_exists);
CREATE INDEX IF NOT EXISTS idx_source_status ON source_entries(status);

CREATE TABLE IF NOT EXISTS upload_batches (
    run_id INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    snapshot_complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS upload_items (
    run_id INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    original_status TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'PENDING',
    uploaded_at TEXT,
    error TEXT,
    PRIMARY KEY (run_id, relative_path)
);
"""


BASELINE_INSERT = """
INSERT INTO baseline_stage (
    relative_path, entry_type, size, mtime_ns, mode, uid, gid,
    link_target, xattr_hash, backed_up_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


SOURCE_UPSERT = """
INSERT INTO source_entries (
    relative_path, entry_type, size, mtime_ns, mode, uid, gid,
    link_target, xattr_hash, source_exists, scan_run_id,
    first_seen_at, last_seen_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
ON CONFLICT(relative_path) DO UPDATE SET
    entry_type=excluded.entry_type,
    size=excluded.size,
    mtime_ns=excluded.mtime_ns,
    mode=excluded.mode,
    uid=excluded.uid,
    gid=excluded.gid,
    link_target=excluded.link_target,
    xattr_hash=excluded.xattr_hash,
    source_exists=1,
    scan_run_id=excluded.scan_run_id,
    last_seen_at=excluded.last_seen_at
"""


def now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def configured_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path.resolve()


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


def entry_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def xattr_fingerprint(path: Path) -> str:
    """兼容旧账本字段；扫描不读取或比较扩展属性。"""
    return ""


def iter_entries(
    root: Path, *, ignored_name: str | None = None
) -> Iterator[tuple[str, os.stat_result, str | None, str]]:
    """扫描一个文件系统，不跟随符号链接或进入其他挂载点。"""
    root_device = os.stat(root, follow_symlinks=False).st_dev
    root_text = os.fspath(root)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as children:
                for child in children:
                    if ignored_name and child.name == ignored_name:
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    path = Path(child.path)
                    rel = os.path.relpath(child.path, root_text)
                    kind = entry_type(info.st_mode)
                    link_target = os.readlink(path) if kind == "symlink" else None
                    yield rel, info, link_target, xattr_fingerprint(path)
                    if kind == "directory" and info.st_dev == root_device:
                        stack.append(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"无法扫描目录 {current}：{exc}") from exc


class Progress:
    def __init__(self, label: str, every: int):
        self.label = label
        self.every = every
        self.started = time.monotonic()

    def show(self, entries: int, files: int, total_bytes: int, final: bool = False) -> None:
        if self.every == 0:
            return
        if not final and entries % self.every != 0:
            return
        elapsed = max(time.monotonic() - self.started, 0.001)
        print(
            f"{self.label}：{entries:,}个条目，{files:,}个文件，"
            f"{format_bytes(total_bytes)}，{entries / elapsed:,.0f}条/秒",
            flush=True,
        )


def open_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existed = db_path.exists()
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise RuntimeError(f"无法设置SQLite日志模式：{mode}")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    conn.commit()
    if not existed:
        os.chmod(db_path, 0o600)
    return conn


def bind_value(
    conn: sqlite3.Connection, key: str, label: str, value: str
) -> None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if row and row["value"] != value:
        raise RuntimeError(f"数据库绑定的{label}是 {row['value']}，不能改为 {value}")
    if not row:
        conn.execute("INSERT INTO metadata(key, value) VALUES(?, ?)", (key, value))
        conn.commit()


def metadata_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def start_run(conn: sqlite3.Connection, action: str) -> int:
    started_at = now_text()
    conn.execute(
        "UPDATE runs SET status='INTERRUPTED', finished_at=? WHERE status='RUNNING'",
        (started_at,),
    )
    cursor = conn.execute(
        "INSERT INTO runs(action, started_at, status) VALUES(?, ?, 'RUNNING')",
        (action, started_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    entries: int = 0,
    files: int = 0,
    total_bytes: int = 0,
    rsync_exit_code: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, entry_count=?, file_count=?, "
        "total_bytes=?, rsync_exit_code=?, error=? WHERE id=?",
        (
            now_text(), status, entries, files, total_bytes,
            rsync_exit_code, error[:1000] if error else None, run_id,
        ),
    )
    conn.commit()


def validate_common(source: Path, db_path: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"源目录不存在：{source}")
    try:
        db_path.relative_to(source)
    except ValueError:
        return
    raise RuntimeError("SQLite数据库不能放在源目录里面")


def validate_disk(
    source: Path, disk_path: Path, db_path: Path, *, dry_run: bool
) -> None:
    validate_common(source, db_path)
    if source == disk_path or source in disk_path.parents or disk_path in source.parents:
        raise RuntimeError("源目录和硬盘备份目录不能相同或互相包含")
    if db_path == disk_path or disk_path in db_path.parents:
        raise RuntimeError("SQLite数据库不能放在备份目录里面")
    if dry_run and not disk_path.is_dir():
        raise RuntimeError(f"dry-run要求目标目录已经存在：{disk_path}")
    if not dry_run:
        disk_path.mkdir(parents=True, exist_ok=True)


def print_command(command: list[str]) -> None:
    print("执行命令：", shlex.join(command), flush=True)


def backup_rsync_command(source: Path, disk_path: Path, dry_run: bool) -> list[str]:
    command = [
        RSYNC_COMMAND,
        "-aHAXx",
        "--numeric-ids",
    ]
    if dry_run:
        command += ["--dry-run", "--itemize-changes", "--stats"]
    else:
        command += [
            "--info=progress2",
            f"--partial-dir={RSYNC_PARTIAL_DIR}",
            "--stats",
        ]
    command += [
        "--",
        os.path.join(os.fspath(source), ""),
        os.path.join(os.fspath(disk_path), ""),
    ]
    return command


def record_baseline(
    conn: sqlite3.Connection,
    disk_path: Path,
    backed_up_at: str,
    batch_size: int,
    progress_every: int,
) -> tuple[int, int, int]:
    """用硬盘实际内容替换数据库基准，失败时保留旧基准。"""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DROP TABLE IF EXISTS baseline_stage")
    conn.execute(
        "CREATE TEMP TABLE baseline_stage AS "
        "SELECT * FROM baseline_entries WHERE 0"
    )
    batch: list[tuple[object, ...]] = []
    entries = files = total_bytes = 0
    progress = Progress("记录硬盘基准", progress_every)
    for rel, info, link_target, xattr_hash in iter_entries(
        disk_path, ignored_name=RSYNC_PARTIAL_DIR
    ):
        kind = entry_type(info.st_mode)
        size = info.st_size if kind == "file" else 0
        batch.append(
            (
                rel, kind, size, info.st_mtime_ns, stat.S_IMODE(info.st_mode),
                info.st_uid, info.st_gid, link_target, xattr_hash, backed_up_at,
            )
        )
        entries += 1
        if kind == "file":
            files += 1
            total_bytes += size
        if len(batch) >= batch_size:
            conn.executemany(BASELINE_INSERT, batch)
            batch.clear()
        progress.show(entries, files, total_bytes)
    if batch:
        conn.executemany(BASELINE_INSERT, batch)
    conn.execute("DELETE FROM baseline_entries")
    conn.execute(
        "INSERT INTO baseline_entries SELECT * FROM baseline_stage"
    )
    conn.execute("DROP TABLE baseline_stage")
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES('baseline_recorded_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (backed_up_at,),
    )
    conn.commit()
    progress.show(entries, files, total_bytes, final=True)
    return entries, files, total_bytes


def import_baseline(
    source: Path,
    disk_path: Path,
    db_path: Path,
    batch_size: int,
    progress_every: int,
) -> None:
    """只读取已有磁盘备份，初始化基准；不复制文件、不覆盖已有基准。"""
    if not disk_path.is_dir():
        raise RuntimeError(f"已有备份目录不存在：{disk_path}")
    validate_disk(source, disk_path, db_path, dry_run=True)
    if next(iter_entries(disk_path, ignored_name=RSYNC_PARTIAL_DIR), None) is None:
        raise RuntimeError("已有备份目录为空，请确认磁盘已挂载且选择了正确的备份目录")
    conn = open_database(db_path)
    run_id = None
    try:
        if metadata_value(conn, "baseline_recorded_at"):
            raise RuntimeError("数据库已经有备份基准，直接扫描变化即可，无需再次导入")
        bind_value(conn, "source_root", "源目录", os.fspath(source))
        bind_value(conn, "disk_root", "硬盘备份目录", os.fspath(disk_path))
        run_id = start_run(conn, "IMPORT_BASELINE")
        print(f"只读取已有磁盘备份：{disk_path}，不复制文件", flush=True)
        entries, files, total_bytes = record_baseline(
            conn, disk_path, now_text(), batch_size, progress_every
        )
        finish_run(conn, run_id, "SUCCESS", entries=entries, files=files,
                   total_bytes=total_bytes)
    except BaseException as exc:
        conn.rollback()
        if run_id is not None:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row and row["status"] == "RUNNING":
                finish_run(conn, run_id, "FAILED", error=str(exc))
        raise
    finally:
        conn.close()
    print(f"已有备份基准导入完成：{db_path}，现在可以扫描或上传增量")


def run_backup(
    source: Path,
    disk_path: Path,
    db_path: Path,
    dry_run: bool,
    batch_size: int,
    progress_every: int,
) -> None:
    validate_disk(source, disk_path, db_path, dry_run=dry_run)
    rsync_path = shutil.which(RSYNC_COMMAND)
    if not rsync_path:
        raise RuntimeError("没有找到rsync，请先安装rsync")
    command = backup_rsync_command(source, disk_path, dry_run)
    command[0] = rsync_path
    print_command(command)
    if dry_run:
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"rsync dry-run失败，退出码={result.returncode}")
        print("dry-run完成：没有复制文件，也没有修改SQLite基准")
        return

    conn = open_database(db_path)
    bind_value(conn, "source_root", "源目录", os.fspath(source))
    bind_value(conn, "disk_root", "硬盘备份目录", os.fspath(disk_path))
    run_id = start_run(conn, "BACKUP")
    try:
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            finish_run(
                conn, run_id, "FAILED", rsync_exit_code=result.returncode,
                error=f"rsync备份失败，退出码={result.returncode}",
            )
            raise RuntimeError(f"rsync备份失败，退出码={result.returncode}")
        backed_up_at = now_text()
        entries, files, total_bytes = record_baseline(
            conn, disk_path, backed_up_at, batch_size, progress_every
        )
        finish_run(
            conn, run_id, "SUCCESS", entries=entries, files=files,
            total_bytes=total_bytes, rsync_exit_code=0,
        )
    except BaseException as exc:
        conn.rollback()
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row and row["status"] == "RUNNING":
            finish_run(conn, run_id, "FAILED", error=str(exc))
        raise
    finally:
        conn.close()
    print(f"全量备份完成，硬盘基准已记录到：{db_path}")


def recompute_status(conn: sqlite3.Connection, scope_table: str | None = None) -> None:
    scope = (
        f" WHERE relative_path IN (SELECT relative_path FROM {scope_table})"
        if scope_table else ""
    )
    conn.execute(
        f"""
        UPDATE source_entries AS s
        SET status = CASE
            WHEN source_exists=0 THEN 'SOURCE_DELETED'
            WHEN EXISTS (
                SELECT 1 FROM baseline_entries AS b
                WHERE b.relative_path=s.relative_path
                  AND b.entry_type=s.entry_type
                  AND b.mode=s.mode AND b.uid=s.uid AND b.gid=s.gid
                  AND (
                    (s.entry_type='file' AND b.size=s.size
                     AND ABS(b.mtime_ns-s.mtime_ns) <= ?)
                    OR s.entry_type='directory'
                    OR (s.entry_type='symlink' AND b.link_target IS s.link_target)
                    OR s.entry_type='other'
                  )
            ) THEN 'DISK_BACKED'
            WHEN cloud_uploaded_at IS NOT NULL
                 AND cloud_entry_type=entry_type
                 AND cloud_mode=mode AND cloud_uid=uid AND cloud_gid=gid
                 AND (
                    (entry_type='file' AND cloud_size=size
                     AND ABS(cloud_mtime_ns-mtime_ns) <= ?)
                    OR entry_type='directory'
                    OR (entry_type='symlink'
                        AND cloud_link_target IS link_target)
                 ) THEN 'CLOUD_UPLOADED'
            WHEN entry_type='other' THEN 'ERROR'
            WHEN last_upload_error IS NOT NULL THEN 'UPLOAD_FAILED'
            WHEN EXISTS (
                SELECT 1 FROM baseline_entries AS b
                WHERE b.relative_path=s.relative_path
            ) OR cloud_uploaded_at IS NOT NULL THEN 'CHANGED'
            ELSE 'NEW'
        END
        {scope}
        """,
        (MTIME_TOLERANCE_NS, MTIME_TOLERANCE_NS),
    )


def scan_source(
    source: Path,
    db_path: Path,
    batch_size: int,
    progress_every: int,
    disk_path: Path | None = None,
) -> int:
    validate_common(source, db_path)
    conn = open_database(db_path)
    bind_value(conn, "source_root", "源目录", os.fspath(source))
    if not metadata_value(conn, "baseline_recorded_at"):
        conn.close()
        if disk_path is None:
            raise RuntimeError("首次扫描需要指定已有磁盘备份目录，以便和服务器文件对比")
        print("还没有备份记录：先扫描已有磁盘备份，再扫描服务器目录进行对比", flush=True)
        import_baseline(source, disk_path, db_path, batch_size, progress_every)
        conn = open_database(db_path)
    run_id = start_run(conn, "SCAN")
    started_at = now_text()
    entries = files = total_bytes = 0
    batch: list[tuple[object, ...]] = []
    progress = Progress("扫描服务器", progress_every)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for rel, info, link_target, xattr_hash in iter_entries(source):
            kind = entry_type(info.st_mode)
            size = info.st_size if kind == "file" else 0
            batch.append(
                (
                    rel, kind, size, info.st_mtime_ns, stat.S_IMODE(info.st_mode),
                    info.st_uid, info.st_gid, link_target, xattr_hash,
                    run_id, started_at, started_at,
                )
            )
            entries += 1
            if kind == "file":
                files += 1
                total_bytes += size
            if len(batch) >= batch_size:
                conn.executemany(SOURCE_UPSERT, batch)
                batch.clear()
            progress.show(entries, files, total_bytes)
        if batch:
            conn.executemany(SOURCE_UPSERT, batch)
        conn.execute(
            "UPDATE source_entries SET source_exists=0 WHERE scan_run_id != ?",
            (run_id,),
        )
        recompute_status(conn)
        conn.commit()
        finish_run(
            conn, run_id, "SUCCESS", entries=entries, files=files,
            total_bytes=total_bytes,
        )
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM source_entries "
                "WHERE source_exists=1 GROUP BY status"
            )
        )
    except BaseException as exc:
        conn.rollback()
        finish_run(conn, run_id, "FAILED", error=str(exc))
        raise
    finally:
        conn.close()
    progress.show(entries, files, total_bytes, final=True)
    print(f"扫描批次：{run_id}")
    print(f"硬盘已备份：{counts.get('DISK_BACKED', 0):,}")
    print(f"云端已上传：{counts.get('CLOUD_UPLOADED', 0):,}")
    print(f"新增文件：{counts.get('NEW', 0):,}")
    print(f"发生变化：{counts.get('CHANGED', 0):,}")
    print(f"等待重试：{counts.get('UPLOAD_FAILED', 0):,}")
    print(f"不支持上传：{counts.get('ERROR', 0):,}")
    print(f"SQLite数据库：{db_path}")
    return run_id


def ssh_base(ssh_path: str, port: int, key: str, password: str) -> list[str]:
    command = [
        ssh_path, "-p", str(port),
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
    ]
    if key:
        command += ["-i", os.fspath(configured_path(key))]
    if password:
        sshpass_path = shutil.which(SSHPASS_COMMAND)
        if not sshpass_path:
            raise RuntimeError("密码认证需要sshpass：yum install -y sshpass")
        command += [
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
        ]
        command = [sshpass_path, "-e"] + command
    return command


def password_environment(password: str) -> dict[str, str] | None:
    if not password:
        return None
    environment = os.environ.copy()
    environment["SSHPASS"] = password
    return environment


def validate_cloud(host: str, user: str, path: str, port: int, bwlimit: int) -> None:
    if not host or not user:
        raise RuntimeError("必须配置云服务器地址和账户")
    if not path.startswith("/") or path == "/":
        raise RuntimeError("云端目录必须是非根目录的绝对路径")
    if any(c in host + user + path for c in "\r\n"):
        raise RuntimeError("云端配置不能包含换行")
    if not 1 <= port <= 65535:
        raise RuntimeError("SSH端口必须在1到65535之间")
    if bwlimit < 0:
        raise RuntimeError("bwlimit不能小于0")


def prepare_cloud(
    ssh_path: str,
    host: str,
    user: str,
    cloud_path: str,
    port: int,
    key: str,
    password: str,
) -> None:
    quoted = shlex.quote(cloud_path)
    remote_command = f"mkdir -p -- {quoted} && test -d {quoted} && test -w {quoted}"
    command = ssh_base(ssh_path, port, key, password) + [
        f"{user}@{host}", remote_command,
    ]
    result = subprocess.run(
        command, check=False, env=password_environment(password)
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法创建云端目录，SSH退出码={result.returncode}")


def validate_selected_paths(paths: object) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise RuntimeError("没有选择部分上传的文件，请先勾选待上传条目")
    for path in paths:
        if (not isinstance(path, str) or "\0" in path
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise RuntimeError("部分上传的文件路径必须是有效的相对路径")
    return list(dict.fromkeys(paths))


def create_upload_candidates(
    conn: sqlite3.Connection, selected_paths: list[str] | None = None,
) -> tuple[int, int]:
    selection_filter = ""
    if selected_paths is not None:
        paths = validate_selected_paths(selected_paths)
        conn.executescript(
            "DROP TABLE IF EXISTS temp.selected_upload_paths;"
            "CREATE TEMP TABLE selected_upload_paths(relative_path TEXT PRIMARY KEY);"
        )
        conn.executemany(
            "INSERT INTO selected_upload_paths VALUES(?)", [(path,) for path in paths]
        )
        selection_filter = " AND relative_path IN (SELECT relative_path FROM selected_upload_paths)"
    conn.executescript(
        f"""
        DROP TABLE IF EXISTS upload_candidates;
        CREATE TEMP TABLE upload_candidates AS
        SELECT relative_path, entry_type, size, mtime_ns, mode, uid, gid,
               link_target, xattr_hash
        FROM source_entries
        WHERE source_exists=1
          AND status IN ('NEW', 'CHANGED', 'UPLOAD_FAILED')
          AND entry_type IN ('file', 'directory', 'symlink'){selection_filter};
        CREATE UNIQUE INDEX upload_candidates_path
          ON upload_candidates(relative_path);
        """
    )
    row = conn.execute(
        "SELECT COUNT(*) AS count, "
        "COALESCE(SUM(CASE WHEN entry_type='file' THEN size ELSE 0 END), 0) AS bytes "
        "FROM upload_candidates"
    ).fetchone()
    return int(row["count"]), int(row["bytes"])


def write_upload_list(conn: sqlite3.Connection, directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix="incremental-upload-", suffix=".list0", dir=directory
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for row in conn.execute(
                "SELECT relative_path FROM upload_candidates ORDER BY relative_path"
            ):
                handle.write(os.fsencode(row["relative_path"]))
                handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def source_matches_candidate(source: Path, row: sqlite3.Row) -> bool:
    path = source / row["relative_path"]
    try:
        info = os.lstat(path)
        kind = entry_type(info.st_mode)
        if kind != row["entry_type"]:
            return False
        if stat.S_IMODE(info.st_mode) != row["mode"]:
            return False
        if info.st_uid != row["uid"] or info.st_gid != row["gid"]:
            return False
        if kind == "file":
            return info.st_size == row["size"] and info.st_mtime_ns == row["mtime_ns"]
        if kind == "symlink":
            return os.readlink(path) == row["link_target"]
        return kind == "directory"
    except OSError:
        return False


def record_upload_result(
    conn: sqlite3.Connection,
    source: Path,
    rsync_exit_code: int,
    uploaded_at: str,
    run_id: int,
) -> tuple[int, int]:
    uploaded = failed = 0
    rows = conn.execute(
        "SELECT * FROM upload_candidates ORDER BY relative_path"
    ).fetchall()
    for row in rows:
        if rsync_exit_code != 0:
            error = f"rsync上传失败，退出码={rsync_exit_code}"
        elif not source_matches_candidate(source, row):
            error = "上传期间源文件发生变化或消失"
        else:
            error = None
        conn.execute(
            "UPDATE upload_items SET result=?, uploaded_at=?, error=? "
            "WHERE run_id=? AND relative_path=?",
            ("FAILED" if error else "UPLOADED", None if error else uploaded_at,
             error, run_id, row["relative_path"]),
        )
        if error:
            conn.execute(
                "UPDATE source_entries SET last_upload_error=? WHERE relative_path=?",
                (error, row["relative_path"]),
            )
            failed += 1
            continue
        conn.execute(
            """
            UPDATE source_entries SET
                cloud_entry_type=?, cloud_size=?, cloud_mtime_ns=?,
                cloud_mode=?, cloud_uid=?, cloud_gid=?, cloud_link_target=?,
                cloud_xattr_hash=?, cloud_uploaded_at=?, last_upload_error=NULL
            WHERE relative_path=?
            """,
            (
                row["entry_type"], row["size"], row["mtime_ns"], row["mode"],
                row["uid"], row["gid"], row["link_target"], row["xattr_hash"],
                uploaded_at, row["relative_path"],
            ),
        )
        uploaded += 1
    return uploaded, failed


def upload_incremental(
    source: Path,
    db_path: Path,
    host: str,
    user: str,
    cloud_path: str,
    port: int,
    key: str,
    password: str,
    bwlimit: int,
    selected_paths: list[str] | None = None,
) -> None:
    validate_cloud(host, user, cloud_path, port, bwlimit)
    if selected_paths is not None:
        selected_paths = validate_selected_paths(selected_paths)
    rsync_path = shutil.which(RSYNC_COMMAND)
    ssh_path = shutil.which(SSH_COMMAND)
    if not rsync_path or not ssh_path:
        raise RuntimeError("需要安装rsync和openssh-clients")
    conn = open_database(db_path)
    destination = f"{user}@{host}:{cloud_path}"
    bind_value(conn, "cloud_destination", "云端目标", destination)
    run_id = start_run(conn, "UPLOAD")
    list_path: Path | None = None
    requested_count = requested_bytes = 0
    try:
        conn.execute(
            "INSERT INTO upload_batches(run_id, destination) VALUES(?, ?)",
            (run_id, destination),
        )
        conn.commit()
        requested_count, requested_bytes = create_upload_candidates(conn, selected_paths)
        # 上传前保存本次文件版本，后续扫描和上传不能覆盖历史批次的清单。
        conn.execute(
            "INSERT INTO upload_items(run_id, relative_path, entry_type, size, "
            "mtime_ns, original_status) "
            "SELECT ?, c.relative_path, c.entry_type, c.size, c.mtime_ns, s.status "
            "FROM upload_candidates AS c JOIN source_entries AS s "
            "ON s.relative_path=c.relative_path",
            (run_id,),
        )
        requested_files = conn.execute(
            "SELECT COUNT(*) FROM upload_candidates WHERE entry_type='file'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE runs SET entry_count=?, file_count=?, total_bytes=? WHERE id=?",
            (requested_count, requested_files, requested_bytes, run_id),
        )
        conn.execute(
            "UPDATE upload_batches SET snapshot_complete=1 WHERE run_id=?", (run_id,)
        )
        conn.commit()
        if requested_count == 0:
            finish_run(conn, run_id, "SUCCESS", rsync_exit_code=0)
            print("选中的条目已无待上传增量，未传输文件" if selected_paths is not None else "没有需要上传的增量文件")
            return
        attempted_at = now_text()
        conn.execute(
            """
            UPDATE source_entries SET
              upload_attempts=upload_attempts+1,
              last_upload_at=?, last_upload_error=NULL
            WHERE relative_path IN (SELECT relative_path FROM upload_candidates)
            """,
            (attempted_at,),
        )
        conn.commit()
        print(
            f"待上传：{requested_count:,}个条目，普通文件{format_bytes(requested_bytes)}",
            flush=True,
        )
        if selected_paths is not None:
            print(f"部分上传：限定{len(selected_paths)}个勾选路径，不递归上传未勾选文件", flush=True)
        prepare_cloud(ssh_path, host, user, cloud_path, port, key, password)
        list_path = write_upload_list(conn, db_path.parent)
        ssh_shell = shlex.join(ssh_base(ssh_path, port, key, password))
        command = [
            rsync_path,
            "-aHAX",
            "--numeric-ids",
            "--dirs",
            "--relative",
            "--from0",
            f"--files-from={list_path}",
            f"--partial-dir={RSYNC_PARTIAL_DIR}",
            "--info=progress2",
            "-e",
            ssh_shell,
        ]
        if bwlimit > 0:
            command.append(f"--bwlimit={bwlimit}")
        command += [
            "--",
            os.path.join(os.fspath(source), ""),
            f"{user}@{host}:{cloud_path.rstrip('/')}/",
        ]
        print_command(command)
        result = subprocess.run(
            command, check=False, env=password_environment(password)
        )
        uploaded_at = now_text()
        conn.execute("BEGIN IMMEDIATE")
        uploaded, failed = record_upload_result(
            conn, source, result.returncode, uploaded_at, run_id
        )
        recompute_status(conn, "upload_candidates")
        conn.commit()
        status = "SUCCESS" if result.returncode == 0 and failed == 0 else "PARTIAL"
        finish_run(
            conn, run_id, status, entries=requested_count,
            files=requested_files, total_bytes=requested_bytes, rsync_exit_code=result.returncode,
            error=None if status == "SUCCESS" else f"仍有{failed}个条目未确认上传",
        )
        print(f"本次确认上传：{uploaded:,}")
        print(f"本次失败：{failed:,}")
        print(f"rsync退出码：{result.returncode}")
        if status != "SUCCESS":
            raise RuntimeError("增量上传没有完全成功，下次运行会重试")
    except BaseException as exc:
        conn.rollback()
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row and row["status"] == "RUNNING":
            conn.execute(
                "UPDATE upload_items SET result='FAILED', error=? "
                "WHERE run_id=? AND result='PENDING'", (str(exc)[:1000], run_id)
            )
            conn.commit()
            counts = conn.execute(
                "SELECT entry_count, file_count, total_bytes FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            finish_run(conn, run_id, "FAILED", entries=counts[0], files=counts[1],
                       total_bytes=counts[2], error=str(exc))
        raise
    finally:
        if list_path:
            list_path.unlink(missing_ok=True)
        conn.close()


def show_status(db_path: Path) -> None:
    if not db_path.is_file():
        raise RuntimeError(f"SQLite数据库不存在：{db_path}")
    conn = open_database(db_path)
    try:
        baseline_count = conn.execute(
            "SELECT COUNT(*) FROM baseline_entries"
        ).fetchone()[0]
        print(f"硬盘基准记录时间：{metadata_value(conn, 'baseline_recorded_at') or '无'}")
        print(f"硬盘基准条目：{int(baseline_count):,}")
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM source_entries "
            "WHERE source_exists=1 GROUP BY status ORDER BY status"
        ):
            print(f"{row['status']}：{int(row['count']):,}")
        latest = conn.execute(
            "SELECT id, action, started_at, finished_at, status, error "
            "FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest:
            print(
                f"最近任务：#{latest['id']} {latest['action']} {latest['status']} "
                f"{latest['started_at']}"
            )
            if latest["error"]:
                print(f"错误：{latest['error']}")
    finally:
        conn.close()


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="backup-scan-upload-") as temp:
        root = Path(temp)
        source = root / "source"
        disk = root / "disk"
        db_path = root / "state.db"
        source.mkdir()
        disk.mkdir()
        (source / "same.txt").write_text("same", encoding="utf-8")
        (source / "changed.txt").write_text("old", encoding="utf-8")
        (source / "empty").mkdir()
        os.symlink("same.txt", source / "same.link")
        shutil.copytree(source, disk, dirs_exist_ok=True, symlinks=True)

        # 旧DELETE账本升级WAL时保留已有记录。
        legacy = sqlite3.connect(db_path)
        legacy.executescript(SCHEMA)
        legacy.execute(
            "INSERT INTO metadata(key, value) VALUES('self_test_legacy', 'retained')"
        )
        legacy.commit()
        assert legacy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        legacy.close()

        # 尚无数据库记录时，一次扫描自动比较已有磁盘与服务器。
        scan_source(source.resolve(), db_path.resolve(), 2, 0, disk.resolve())

        # 保存基准后移除测试硬盘，验证后续扫描完全依赖SQLite。
        shutil.rmtree(disk)
        scan_source(source.resolve(), db_path.resolve(), 2, 0)
        conn = open_database(db_path)
        # 旧账本的NFS与本地扩展属性指纹不同，仍应识别为已备份。
        conn.execute("UPDATE baseline_entries SET xattr_hash='legacy-nfs-acl'")
        conn.execute(
            "UPDATE source_entries SET xattr_hash='legacy-selinux', status='CHANGED'"
        )
        recompute_status(conn)
        conn.commit()
        statuses = dict(
            conn.execute("SELECT relative_path, status FROM source_entries")
        )
        assert set(statuses.values()) == {"DISK_BACKED"}

        # 模拟大量未提交写入触发缓存写盘，页面仍可读上次提交的清单。
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert metadata_value(conn, "self_test_legacy") == "retained"
        conn.execute("PRAGMA cache_size=10")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [(f"self_test_pending_{i}", "x" * 4096) for i in range(128)],
        )
        reader = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
        try:
            assert reader.execute("SELECT COUNT(*) FROM source_entries").fetchone()[0] == len(statuses)
            assert reader.execute(
                "SELECT COUNT(*) FROM metadata WHERE key LIKE 'self_test_pending_%'"
            ).fetchone()[0] == 0
        finally:
            reader.close()
            conn.rollback()
        conn.close()

        time.sleep(0.01)
        (source / "changed.txt").write_text("new-content", encoding="utf-8")
        (source / "new.txt").write_text("new", encoding="utf-8")
        scan_source(source.resolve(), db_path.resolve(), 2, 0)
        conn = open_database(db_path)
        statuses = dict(
            conn.execute("SELECT relative_path, status FROM source_entries")
        )
        assert statuses["same.txt"] == "DISK_BACKED"
        assert statuses["changed.txt"] == "CHANGED"
        assert statuses["new.txt"] == "NEW"
        count, _ = create_upload_candidates(conn)
        assert count == 2

        # 部分上传只取勾选且仍待上传的路径，空选择不能退回全部上传。
        count, _ = create_upload_candidates(conn, ['new.txt', 'new.txt'])
        assert count == 1
        assert [row[0] for row in conn.execute('SELECT relative_path FROM upload_candidates')] == ['new.txt']
        assert create_upload_candidates(conn, ['same.txt'])[0] == 0
        for invalid in ([], ['../outside.txt'], ['/absolute.txt'], [''], [None]):
            try:
                validate_selected_paths(invalid)
            except RuntimeError:
                pass
            else:
                raise AssertionError('部分上传接受了空选择或非法路径')
        assert create_upload_candidates(conn)[0] == 2

        # 旧上传记录也忽略扩展属性；上传后的版本确认仍检查基本属性。
        conn.execute("""
            UPDATE source_entries SET
                cloud_entry_type=entry_type, cloud_size=size,
                cloud_mtime_ns=mtime_ns, cloud_mode=mode,
                cloud_uid=uid, cloud_gid=gid, cloud_link_target=link_target,
                cloud_xattr_hash='legacy-cloud', cloud_uploaded_at=?,
                xattr_hash='legacy-source'
            WHERE relative_path='changed.txt'
        """, (now_text(),))
        recompute_status(conn)
        row = conn.execute(
            "SELECT * FROM source_entries WHERE relative_path='changed.txt'"
        ).fetchone()
        assert row["status"] == "CLOUD_UPLOADED"
        assert source_matches_candidate(source, row)
        os.chmod(source / "changed.txt", row["mode"] ^ stat.S_IXUSR)
        assert not source_matches_candidate(source, row)
        conn.close()
    print("自检通过")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="全量备份到硬盘，记录基准，并上传后续增量文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "action", choices=("backup", "import-baseline", "scan", "upload", "status", "self-test")
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览硬盘备份差异")
    parser.add_argument("--source", default=SOURCE_PATH, help="服务器源目录")
    parser.add_argument("--disk", default=DISK_BACKUP_PATH, help="硬盘备份目录")
    parser.add_argument("--db", default=DATABASE_PATH, help="SQLite数据库路径")
    parser.add_argument("--cloud-host", default=CLOUD_HOST, help="云服务器地址")
    parser.add_argument("--cloud-user", default=CLOUD_USER, help="云服务器账户")
    parser.add_argument("--cloud-path", default=CLOUD_PATH, help="云端挂载目录")
    parser.add_argument("--ssh-port", type=int, default=CLOUD_SSH_PORT)
    parser.add_argument("--ssh-key", default=CLOUD_SSH_KEY)
    parser.add_argument("--bwlimit", type=int, default=CLOUD_BWLIMIT, help="限速KB/s，0不限速")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--progress-every", type=int, default=PROGRESS_EVERY)
    args = parser.parse_args()
    if args.dry_run and args.action != "backup":
        parser.error("--dry-run只能和backup一起使用")
    if args.batch_size < 1 or args.progress_every < 0 or args.bwlimit < 0:
        parser.error("batch-size必须大于0，progress-every和bwlimit不能小于0")
    return args


def main() -> int:
    args = parse_args()
    if args.action == "self-test":
        self_test()
        return 0
    db_path = configured_path(args.db)
    if args.action == "status":
        show_status(db_path)
        return 0
    source = configured_path(args.source)
    if args.action == "import-baseline":
        import_baseline(
            source, configured_path(args.disk), db_path,
            args.batch_size, args.progress_every,
        )
        return 0
    if args.action == "backup":
        run_backup(
            source,
            configured_path(args.disk),
            db_path,
            args.dry_run,
            args.batch_size,
            args.progress_every,
        )
        return 0
    if args.action == "scan":
        scan_source(source, db_path, args.batch_size, args.progress_every,
                    configured_path(args.disk))
        return 0
    scan_source(source, db_path, args.batch_size, args.progress_every,
                configured_path(args.disk))
    upload_incremental(
        source,
        db_path,
        args.cloud_host,
        args.cloud_user,
        args.cloud_path,
        args.ssh_port,
        args.ssh_key,
        CLOUD_PASSWORD,
        args.bwlimit,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
