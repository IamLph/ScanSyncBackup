"""在独立进程中调用原脚本；配置从标准输入接收，密码不进入命令行。"""
import importlib.util
import json
import sys
from pathlib import Path

from config import BACKUP_SCRIPT


def load_engine():
    spec = importlib.util.spec_from_file_location("migration_engine", BACKUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(action, settings, password=""):
    engine = load_engine()
    source = Path(settings["source"]).resolve()
    disk = Path(settings["disk"]).resolve()
    db = Path(settings["db"]).resolve()
    batch = settings["batch_size"]
    progress = settings["progress_every"]
    if action in ("backup", "preview"):
        engine.run_backup(source, disk, db, action == "preview", batch, progress)
    elif action == "import-baseline":
        engine.import_baseline(source, disk, db, batch, progress)
    elif action in ("scan", "upload"):
        engine.scan_source(source, db, batch, progress, disk)
        if action == "upload":
            engine.upload_incremental(
                source, db, settings["cloud_host"], settings["cloud_user"],
                settings["cloud_path"], settings["ssh_port"], settings["ssh_key"],
                password or ("" if settings["ssh_key"] else engine.CLOUD_PASSWORD), settings["bwlimit"],
            )
    else:
        raise ValueError("不支持的任务")


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        execute(payload["action"], payload["settings"], payload.get("password", ""))
    except Exception as exc:
        print(f"任务失败：{exc}", flush=True)
        sys.exit(1)
