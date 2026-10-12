"""文件迁移操作台：单实例 Flask 服务，耗时任务由独立进程执行。"""
import argparse
import atexit
import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import sqlite3
    if sqlite3.sqlite_version_info < (3, 22, 0):
        import pysqlite3 as sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3

from flask import Flask, jsonify, render_template, request, session

import config
from runner import load_engine

STATUSES = {
    "DISK_BACKED": "硬盘已备份", "NEW": "新增待上传", "CHANGED": "变化待上传",
    "CLOUD_UPLOADED": "云端已上传", "UPLOAD_FAILED": "上传待重试",
    "SOURCE_DELETED": "源端已删除", "ERROR": "不支持的文件",
}
ACTIONS = {"preview": "预览备份", "backup": "备份到硬盘", "import-baseline": "导入已有磁盘备份", "scan": "扫描变化", "upload": "上传增量"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def write_settings(path, value):
    stored = dict(value)
    stored['db'] = Path(value['db']).relative_to(path.parent.resolve()).as_posix()
    write_json(path, stored)


def defaults(state_dir=None):
    engine = load_engine()
    directory = Path(state_dir or config.STATE_DIR).resolve()
    database = engine.configured_path(config.DATABASE_PATH) if config.DATABASE_PATH else directory / 'backup_state.db'
    if directory not in database.parents:
        raise ValueError('数据库必须位于 data 目录中，请检查 VIEW_DATABASE_PATH')
    return {
        "source": str(engine.configured_path(config.SOURCE_PATH or engine.SOURCE_PATH)),
        "disk": str(engine.configured_path(config.DISK_PATH or engine.DISK_BACKUP_PATH)),
        "db": str(database),
        "cloud_host": engine.CLOUD_HOST, "cloud_user": engine.CLOUD_USER,
        "cloud_path": engine.CLOUD_PATH, "ssh_port": engine.CLOUD_SSH_PORT,
        "ssh_key": engine.CLOUD_SSH_KEY, "bwlimit": engine.CLOUD_BWLIMIT,
        "batch_size": engine.BATCH_SIZE, "progress_every": 1000,
    }


def validate_settings(value, state_dir=None):
    expected = defaults(state_dir)
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("配置字段不完整或包含未知字段")
    result = {}
    for key, default in expected.items():
        item = value[key]
        if isinstance(default, int):
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{key} 必须是整数")
        elif not isinstance(item, str) or any(c in item for c in "\0\r\n"):
            raise ValueError(f"{key} 格式不正确")
        result[key] = item.strip() if isinstance(item, str) else item
    for key in ("source", "disk", "db"):
        path = Path(result[key])
        if not path.is_absolute():
            if key != 'db':
                raise ValueError("源目录和硬盘目录必须使用绝对路径")
            path = Path(state_dir or config.STATE_DIR) / path
        result[key] = str(path.resolve())
    for key, locked in (('db', True), ('source', bool(config.SOURCE_PATH)), ('disk', bool(config.DISK_PATH))):
        if locked and result[key] != expected[key]:
            raise ValueError(f"{key} 路径由服务配置固定为 {expected[key]}，不能通过页面修改")
    source, disk, db = (Path(result[k]) for k in ("source", "disk", "db"))
    if source == disk or source in disk.parents or disk in source.parents:
        raise ValueError("源目录和硬盘目录不能相同或互相包含")
    if db == source or source in db.parents or db == disk or disk in db.parents:
        raise ValueError("数据库不能放在源目录或硬盘备份目录内")
    if not 1 <= result["ssh_port"] <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")
    if result["batch_size"] < 1 or result["progress_every"] < 1 or result["bwlimit"] < 0:
        raise ValueError("批量大小和进度间隔必须大于 0，限速不能小于 0")
    if result["ssh_key"] and not Path(result["ssh_key"]).is_absolute():
        raise ValueError("SSH 私钥请使用绝对路径")
    if not result["cloud_host"] or not result["cloud_user"]:
        raise ValueError("云端地址和用户不能为空")
    if result["cloud_host"].startswith("-") or result["cloud_user"].startswith("-"):
        raise ValueError("云端地址或用户格式不正确")
    if not result["cloud_path"].startswith("/") or result["cloud_path"] == "/":
        raise ValueError("云端目录必须是非根目录的绝对路径")
    return result


def read_db(settings):
    path = Path(settings["db"])
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.3)
    connection.row_factory = sqlite3.Row
    return connection


def increment_records(connection, run_id=None):
    """读取批次汇总；旧账本没有文件快照时明确返回明细不可用。"""
    where = "action='UPLOAD'" + (" AND id=?" if run_id is not None else "")
    arguments = (run_id,) if run_id is not None else ()
    runs = connection.execute(f"SELECT * FROM runs WHERE {where} ORDER BY id", arguments).fetchall()
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    details = {}
    if {'upload_batches', 'upload_items'} <= tables:
        batch_where = " WHERE b.run_id=?" if run_id is not None else ""
        rows = connection.execute(
            "SELECT b.run_id,b.destination,b.snapshot_complete,COUNT(i.relative_path) AS item_count,"
            "COALESCE(SUM(i.entry_type='file'),0) AS file_count,"
            "COALESCE(SUM(i.entry_type='file' AND i.original_status='NEW'),0) AS new_files,"
            "COALESCE(SUM(i.entry_type='file' AND i.original_status='CHANGED'),0) AS changed_files,"
            "COALESCE(SUM(i.entry_type='file' AND i.original_status='UPLOAD_FAILED'),0) AS retry_files,"
            "COALESCE(SUM(i.entry_type='file' AND i.result='UPLOADED'),0) AS uploaded_files,"
            "COALESCE(SUM(CASE WHEN i.entry_type='file' AND i.result='UPLOADED' THEN i.size ELSE 0 END),0) AS uploaded_bytes,"
            "COALESCE(SUM(i.result='FAILED'),0) AS failed_items,"
            "COALESCE(SUM(i.result='PENDING'),0) AS pending_items "
            "FROM upload_batches b LEFT JOIN upload_items i ON i.run_id=b.run_id"
            f"{batch_where} GROUP BY b.run_id", arguments,
        ).fetchall()
        details = {row['run_id']: dict(row) for row in rows}
    result = []
    for row in runs:
        record = dict(row)
        detail = details.get(row['id'])
        complete = bool(detail and detail['snapshot_complete'])
        record.update(
            has_details=complete, destination=detail['destination'] if detail else None,
            size_known=complete or (not detail and row['status'] in ('SUCCESS', 'PARTIAL')),
            file_count=detail['file_count'] if complete else None,
            new_files=detail['new_files'] if complete else None,
            changed_files=detail['changed_files'] if complete else None,
            retry_files=detail['retry_files'] if complete else None,
            uploaded_files=detail['uploaded_files'] if complete else None,
            uploaded_bytes=detail['uploaded_bytes'] if complete else (row['total_bytes'] if row['status'] == 'SUCCESS' else None),
            failed_items=detail['failed_items'] if complete else None,
            pending_items=detail['pending_items'] if complete else None,
        )
        result.append(record)
    return result


class Tasks:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.process = None
        self.task = None
        records = sorted(directory.glob("*.json"))
        if records:
            self.task = json.loads(records[-1].read_text(encoding="utf-8"))
            if self.task["status"] == "running":
                self.task.update(status="interrupted", finished_at=now(), error="服务退出，任务未确认完成；旧任务执行锁释放后可以重新运行")
                self.persist()

    def persist(self):
        write_json(self.directory / f"{self.task['id']}.json", self.task)

    def active(self):
        return self.task and self.task["status"] == "running"

    def snapshot(self):
        with self.lock:
            return dict(self.task) if self.task else None

    def start(self, action, settings, password):
        with self.lock:
            if self.active():
                raise ValueError("已有任务正在运行，请等待完成或先停止")
            operation_lock = (self.directory / "operation.lock").open("a")
            try:
                fcntl.flock(operation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                operation_lock.close()
                raise ValueError("上一次后台任务仍在运行，请等待它完成后重试")
            identifier = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            task = {
                "id": identifier, "action": action, "label": ACTIONS[action],
                "status": "running", "started_at": now(), "finished_at": None,
                "exit_code": None, "error": None, "settings": dict(settings),
            }
            self.task = task
            self.persist()
            log_path = self.directory / f"{identifier}.log"
            try:
                with log_path.open("wb") as log:
                    log_path.chmod(0o600)
                    process = subprocess.Popen(
                        [sys.executable, "-u", str(config.PROJECT_DIR / "runner.py")],
                        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True, pass_fds=(operation_lock.fileno(),),
                    )
                    self.process = process
                    payload = {"action": action, "settings": settings, "password": password}
                    process.stdin.write(json.dumps(payload).encode("utf-8"))
                    process.stdin.close()
                threading.Thread(target=self.monitor, args=(process, task), daemon=True).start()
            except Exception as exc:
                if self.process and self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=10)
                task.update(status="failed", finished_at=now(), error=str(exc))
                self.persist()
                raise
            finally:
                # 后台进程继承此锁；即使 Web 服务被强制结束，也不会同时启动第二个任务。
                operation_lock.close()
            return dict(task)

    def monitor(self, process, task):
        code = process.wait()
        with self.lock:
            if task.get("cancel_requested"):
                return  # 停止请求负责收尾，期间仍禁止启动新任务。
            if task["status"] == "running":
                task["status"] = "success" if code == 0 else "failed"
            task.update(exit_code=code, finished_at=now())
            if task["status"] == "failed":
                task["error"] = "任务未完成，请查看运行日志"
            self.persist()

    def cancel(self):
        with self.lock:
            if not self.active() or not self.process:
                return
            process = self.process
            task = self.task
            task["cancel_requested"] = True
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # 保持 running，直到整个进程组停止后，才允许下一个任务。
            task["error"] = "已请求停止"
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        # 主进程已经退出时，仍可能有忽略 SIGTERM 的 rsync/ssh 子进程。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        with self.lock:
            task.update(status="cancelled", finished_at=now(), exit_code=process.returncode)
            self.persist()


def create_app(state_dir=None):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=64 * 1024, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)
    directory = Path(state_dir or config.STATE_DIR)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings_path = directory / "settings.json"
    settings = defaults(directory)
    if settings_path.exists():
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        repaired = False
        if isinstance(saved, dict) and isinstance(saved.get('db'), str):
            old_path = Path(saved['db'])
            repaired = old_path.is_absolute()
            if not old_path.is_absolute():
                old_path = directory / old_path
            old_path = old_path.resolve()
            target_path = Path(settings['db'])
            if old_path != target_path:
                if old_path.is_file():
                    raise RuntimeError(
                        f"旧账本仍位于 {old_path}，请先将其迁移到 {target_path} 并更新 settings.json 的 db；"
                        "为保留已有备份和上传记录，未自动切换数据库"
                    )
                saved['db'] = settings['db']
                repaired = True
        if isinstance(saved, dict):
            for key, locked in (('source', bool(config.SOURCE_PATH)), ('disk', bool(config.DISK_PATH))):
                if locked and saved.get(key) != settings[key]:
                    saved[key] = settings[key]
                    repaired = True
        settings = validate_settings(saved, directory)
        if repaired:
            write_settings(settings_path, settings)
    # 在接收页面读取请求前升级已有账本；无账本时仍等首次任务创建。
    database_path = Path(settings['db'])
    if database_path.is_file():
        connection = load_engine().open_database(database_path)
        connection.close()
    tasks = Tasks(directory / "tasks")
    app.extensions["migration_tasks"] = tasks
    app.extensions["migration_settings"] = settings
    atexit.register(tasks.cancel)
    # 密码仅在服务内存中按浏览器会话保存，Cookie 中只有随机会话标识。
    cloud_credentials = {}

    def connection_identity():
        return tuple(settings[key] for key in ('cloud_host', 'cloud_user', 'ssh_port'))

    def saved_password():
        entry = cloud_credentials.get(session.get('csrf'))
        return entry[1] if entry and entry[0] == connection_identity() else ''

    def validate_password(password):
        if not isinstance(password, str) or len(password) > 4096 or '\0' in password:
            raise ValueError('密码格式不正确')
        return password

    def require_cloud_credentials(password):
        if not password and not settings['ssh_key'] and not load_engine().CLOUD_PASSWORD:
            raise ValueError('尚未提供云端密码，请填写密码并保存云端连接。手工 SSH 登录不会把密码传给本程序。')

    @app.before_request
    def check_mutation():
        if request.method == "POST":
            token = session.get("csrf")
            if not token or not secrets.compare_digest(token, request.headers.get("X-CSRF-Token", "")):
                return jsonify(error="页面会话已过期，请刷新后再操作"), 403

    @app.errorhandler(ValueError)
    @app.errorhandler(RuntimeError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(sqlite3.Error)
    def database_error(exc):
        return jsonify(error=f"数据库暂时无法读取：{exc}"), 503

    @app.get("/")
    def index():
        session.setdefault("csrf", secrets.token_hex(32))
        return render_template("index.html", csrf=session["csrf"], statuses=STATUSES)

    @app.get("/settings")
    def settings_page():
        session.setdefault("csrf", secrets.token_hex(32))
        return render_template("settings.html", csrf=session["csrf"])

    @app.get("/increments")
    def increments_page():
        return render_template("increments.html")

    def display_increment_status(records):
        task = tasks.snapshot()
        for record in records:
            if record['status'] == 'RUNNING':
                active = (task and task['action'] == 'upload' and task['status'] == 'running'
                          and task['settings']['db'] == settings['db']
                          and task['started_at'] <= record['started_at'])
                if not active:
                    record['status'] = 'INTERRUPTED'
        return records

    @app.get("/api/increments")
    def increments():
        page = max(1, int(request.args.get('page', 1)))
        limit = 20
        connection = read_db(settings)
        if not connection:
            return jsonify(batches=[], trend=[], total=0, page=1, page_size=limit, db=settings['db'])
        try:
            connection.execute('BEGIN')
            records = display_increment_status(increment_records(connection))
            for number, record in enumerate(records, 1):
                record['number'] = number
            page = min(page, max(1, (len(records) + limit - 1) // limit))
            batches = list(reversed(records))[(page - 1) * limit:page * limit]
            trend = [
                {key: record[key] for key in ('id', 'number', 'started_at', 'total_bytes', 'size_known', 'status')}
                for record in records
            ]
            return jsonify(batches=batches, trend=trend, total=len(records), page=page, page_size=limit, db=settings['db'])
        finally:
            connection.close()

    @app.get('/api/increments/<int:run_id>')
    def increment_detail(run_id):
        page = max(1, int(request.args.get('page', 1)))
        search = request.args.get('q', '').strip()
        origin = request.args.get('origin', '')
        if origin not in ('', 'NEW', 'CHANGED', 'UPLOAD_FAILED'):
            raise ValueError('未知的增量类型')
        limit = 30
        connection = read_db(settings)
        if not connection:
            return jsonify(error='找不到此上传批次'), 404
        try:
            connection.execute('BEGIN')
            records = display_increment_status(increment_records(connection, run_id))
            if not records:
                return jsonify(error='找不到此上传批次'), 404
            batch = records[0]
            batch['number'] = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE action='UPLOAD' AND id<=?", (run_id,)
            ).fetchone()[0]
            if not batch['has_details']:
                return jsonify(batch=batch, items=[], total=0, page=1, page_size=limit)
            filters, arguments = ['run_id=?'], [run_id]
            if search:
                filters.append("relative_path LIKE ? ESCAPE '\\'")
                arguments.append('%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            if origin:
                filters.append('original_status=?')
                arguments.append(origin)
            where = ' AND '.join(filters)
            total = connection.execute(f'SELECT COUNT(*) FROM upload_items WHERE {where}', arguments).fetchone()[0]
            page = min(page, max(1, (total + limit - 1) // limit))
            rows = connection.execute(
                f'SELECT * FROM upload_items WHERE {where} ORDER BY relative_path LIMIT ? OFFSET ?',
                [*arguments, limit, (page - 1) * limit],
            ).fetchall()
            return jsonify(batch=batch, items=[dict(row) for row in rows], total=total, page=page, page_size=limit)
        finally:
            connection.close()

    @app.get("/api/config")
    def get_config():
        return jsonify(settings=settings, configured=settings_path.exists(),
                       locked_paths={'source': bool(config.SOURCE_PATH), 'disk': bool(config.DISK_PATH)},
                       password_configured=bool(saved_password()), engine=str(config.BACKUP_SCRIPT))

    @app.post("/api/config")
    def save_config():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise ValueError('配置格式不正确')
        payload = dict(payload)
        password = validate_password(payload.pop('cloud_password', ''))
        updated = validate_settings(payload, directory)
        with tasks.lock:
            if tasks.active():
                return jsonify(error="任务运行中，暂时不能修改配置"), 409
            write_settings(settings_path, updated)
            settings.clear()
            settings.update(updated)
            if password:
                cloud_credentials[session['csrf']] = (connection_identity(), password)
        return jsonify(settings=settings, password_configured=bool(saved_password()))

    @app.post('/api/cloud-test')
    def cloud_test():
        with tasks.lock:
            if tasks.active():
                return jsonify(error='任务运行中，请完成或停止后再测试连接'), 409
            password = saved_password()
            require_cloud_credentials(password)
            engine = load_engine()
            ssh = shutil.which(engine.SSH_COMMAND)
            if not ssh:
                raise ValueError('没有找到 SSH 客户端')
            command = engine.ssh_base(ssh, settings['ssh_port'], settings['ssh_key'], password)
            command += [f"{settings['cloud_user']}@{settings['cloud_host']}", 'true']
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=25, env=engine.password_environment(password))
            except subprocess.TimeoutExpired:
                raise ValueError('SSH 连接超时，请检查云端 IP、端口和网络')
            if result.returncode:
                hints = {5: '云端用户名或密码认证失败', 6: '云服务器指纹尚未确认',
                         7: '云服务器指纹发生变化', 255: 'SSH 连接或认证被拒绝'}
                hint = hints.get(result.returncode, 'SSH 连接失败')
                raise ValueError(f"{hint}，退出码={result.returncode}。{result.stderr.strip()[:1000]}")
        return jsonify(message='SSH 连接测试成功；测试未创建目录或上传文件')

    @app.get("/api/summary")
    def summary():
        result = dict(baseline_at=None, baseline_count=0, counts={}, total_bytes=0, latest_run=None, db_exists=False)
        connection = read_db(settings)
        if connection:
            try:
                result["db_exists"] = True
                row = connection.execute("SELECT value FROM metadata WHERE key='baseline_recorded_at'").fetchone()
                result["baseline_at"] = row[0] if row else None
                result["baseline_count"] = connection.execute("SELECT COUNT(*) FROM baseline_entries").fetchone()[0]
                result["counts"] = dict(connection.execute("SELECT status,COUNT(*) FROM source_entries GROUP BY status"))
                result["total_bytes"] = connection.execute(
                    "SELECT COALESCE(SUM(size),0) FROM source_entries WHERE source_exists=1 AND entry_type='file'"
                ).fetchone()[0]
                row = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
                result["latest_run"] = dict(row) if row else None
            finally:
                connection.close()
        result["task"] = tasks.snapshot()
        return jsonify(result)

    @app.get("/api/files")
    def files():
        page = max(1, int(request.args.get("page", 1)))
        limit = 30
        status = request.args.get("status", "")
        search = request.args.get("q", "").strip()
        if status and status not in STATUSES:
            raise ValueError("未知的文件状态")
        connection = read_db(settings)
        if not connection:
            return jsonify(items=[], total=0, page=page, page_size=limit, origin="source")
        try:
            source_count = connection.execute("SELECT COUNT(*) FROM source_entries").fetchone()[0]
            table = "source_entries" if source_count else "baseline_entries"
            filters, arguments = [], []
            if search:
                filters.append("relative_path LIKE ? ESCAPE '\\'")
                arguments.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
            if status:
                filters.append("status=?" if source_count else "?='DISK_BACKED'")
                arguments.append(status)
            where = " WHERE " + " AND ".join(filters) if filters else ""
            total = connection.execute(f"SELECT COUNT(*) FROM {table}{where}", arguments).fetchone()[0]
            page = min(page, max(1, (total + limit - 1) // limit))
            columns = "relative_path,entry_type,size,mtime_ns,mode"
            columns += ",status,cloud_uploaded_at,last_upload_error" if source_count else ", 'DISK_BACKED' AS status, NULL AS cloud_uploaded_at, NULL AS last_upload_error"
            rows = connection.execute(
                f"SELECT {columns} FROM {table}{where} ORDER BY relative_path LIMIT ? OFFSET ?",
                [*arguments, limit, (page - 1) * limit],
            ).fetchall()
            return jsonify(items=[dict(row) for row in rows], total=total, page=page, page_size=limit, origin="source" if source_count else "baseline")
        finally:
            connection.close()

    @app.post("/api/tasks")
    def start_task():
        payload = request.get_json()
        if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
            raise ValueError("未知的任务")
        password = validate_password(payload.get("password", "")) or saved_password()
        with tasks.lock:
            if tasks.active():
                return jsonify(error="已有任务正在运行"), 409
            if not settings_path.exists():
                raise ValueError("请先在界面保存迁移配置，再手动开始任务")
            engine = load_engine()
            engine.validate_common(Path(settings["source"]), Path(settings["db"]))
            if payload["action"] == "upload":
                require_cloud_credentials(password)
                connection = read_db(settings)
                try:
                    row = connection.execute("SELECT value FROM metadata WHERE key='baseline_recorded_at'").fetchone() if connection else None
                finally:
                    if connection:
                        connection.close()
                if not row:
                    raise ValueError("请先点击开始扫描，对比已有磁盘备份并建立记录，再上传增量")
            return jsonify(task=tasks.start(payload["action"], dict(settings), password)), 202

    @app.post("/api/tasks/cancel")
    def cancel_task():
        tasks.cancel()
        return jsonify(task=tasks.snapshot())

    @app.get("/api/logs")
    def logs():
        task = tasks.snapshot()
        if not task:
            return jsonify(text="", cursor=0, task_id=None)
        cursor = max(0, int(request.args.get("cursor", 0)))
        if request.args.get("task_id") != task["id"]:
            cursor = 0
        path = tasks.directory / f"{task['id']}.log"
        if not path.exists():
            return jsonify(text="", cursor=0, task_id=task["id"])
        with path.open("rb") as stream:
            stream.seek(min(cursor, path.stat().st_size))
            content = stream.read(64 * 1024)
            # 避免把一个中文 UTF-8 字符截成两段。
            consumed = len(content)
            try:
                decoded = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    consumed = exc.start
                decoded = content[:consumed].decode("utf-8", errors="replace")
            cursor = stream.tell() - len(content) + consumed
        return jsonify(text=decoded, cursor=cursor, task_id=task["id"])

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="文件迁移可视化操作台")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    config.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance_lock = (config.STATE_DIR / "server.lock").open("a")
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("此配置目录已有操作台实例在运行")
    try:
        application = create_app()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    application.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
