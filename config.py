"""Web 服务配置。也可以用环境变量或启动参数覆盖监听地址和端口。"""
import os
from pathlib import Path

HOST = os.environ.get("VIEW_HOST", "127.0.0.1")
PORT = int(os.environ.get("VIEW_PORT", "5080"))
PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("VIEW_STATE_DIR", str(PROJECT_DIR / "data"))).resolve()
BACKUP_SCRIPT = Path(os.environ.get(
    "VIEW_BACKUP_SCRIPT", str(PROJECT_DIR / "scan_sync_backup.py")
)).resolve()
# Compose 设置的源目录和磁盘目录在页面只读，数据库固定放在数据目录。
SOURCE_PATH = os.environ.get("VIEW_SOURCE_PATH")
DISK_PATH = os.environ.get("VIEW_DISK_PATH")
DATABASE_PATH = os.environ.get("VIEW_DATABASE_PATH")
