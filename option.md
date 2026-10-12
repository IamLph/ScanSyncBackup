# ScanSyncBackup 文件迁移工作台

`scan_sync_backup.py` 负责磁盘备份、扫描变化、上传云端增量；Flask 提供操作页面。整个 `ScanSyncBackup` 目录可独立部署，也可以只运行脚本。

下面使用示例地址说明，部署时按自己的服务器配置：

| 用途 | IP | 文件目录 |
| --- | --- | --- |
| 源服务器，运行工作台或脚本 | `10.90.0.2` | `/files/file_server` |
| 硬盘服务器，CentOS，提供 NFS 共享 | `10.90.0.3` | `/data/file_server` |
| 云服务器，接收上传 | `10.90.0.4` | `/files/file_server` |

源服务器把硬盘服务器的硬盘目录挂到 `/mnt/files`，就能像访问本机目录一样读取备份。先按第 3 节挂好 NFS，再部署工作台。已有硬盘备份直接扫描建立账本；云端先放好全量文件，之后脚本只上传新增和变化的文件。

## 1. docker-compose 构建与运行

在源服务器 `10.90.0.2` 上安装 Docker 和 `docker-compose`。下载或克隆项目后，进入 `ScanSyncBackup` 目录；项目可以放在任意位置。后面的项目命令都在这个目录执行，所有容器配置直接写在 `docker-compose.yml`，不需要 `.env`。

```bash
cd ScanSyncBackup
mkdir -p data ssh
chmod 700 ssh
```

修改 Compose 中这些配置：

| 配置 | 本次使用的值 |
| --- | --- |
| 访问端口 | `"5080:80"`，改左边的 `5080`，容器内部默认 `80` |
| 服务器源目录 | `VIEW_SOURCE_PATH: "/files/file_server"`，同时修改对应挂载的 `source`、`target` |
| 磁盘备份目录 | `VIEW_DISK_PATH: "/mnt/files"`，同时修改对应挂载的 `source`、`target` |
| 数据持久化 | `./data` 挂载到 `/app/data`，数据库固定为 `./data/backup_state.db`，`settings.json` 中的 `db` 保存为 `backup_state.db`，相对于该配置文件所在目录解析 |

Compose 里源目录对应的挂载也改为：

```yaml
- type: bind
  source: /files/file_server
  target: /files/file_server
  read_only: true
  bind:
    create_host_path: false
```

备份目录的 `source` 和 `target` 都保持 `/mnt/files`，`data` 和 `ssh` 挂载保持原配置。先确认 NFS 已挂好：

```bash
mountpoint /mnt/files
```

```bash
docker-compose config                  # 检查配置
docker-compose build                   # 制作镜像
docker-compose up -d                   # 启动
docker-compose logs -f migration        # 查看日志
```

访问 `http://10.90.0.2:5080`，服务器防火墙需放行配置的访问端口。更新代码后重新执行 `docker-compose build` 和 `docker-compose up -d`；停止使用 `docker-compose down`。

### 首次启动：进入容器确认 SSH 指纹

首次上传前执行一次，下面使用云端 `root` 账户和 SSH 端口 `22`：

```bash
docker-compose exec migration bash     # 进入容器
ssh -p 22 root@10.90.0.4              # 在容器内连接云服务器
```

出现 `Are you sure you want to continue connecting (yes/no/[fingerprint])?` 时，确认是目标服务器后输入 `yes`，再输入云服务器密码。登录成功后依次退出：

```bash
exit                                  # 退出云服务器，回到容器
exit                                  # 退出容器，回到部署服务器
```

指纹保存到宿主机 `./ssh/known_hosts`，保留此目录后重建容器通常无需再次确认。这一步只记住服务器指纹，不保存登录密码；回到页面仍需填写云端密码，再「测试连接」和「开始上传」。`SSH 退出码=6` 通常表示尚未确认指纹。

页面分为「控制台」「全局配置」「同步增量记录」。控制台内通过「操作台」「文件清单」「运行日志」切换视图，每次只展示当前视图，不用上下拖动查找；路径与云端连接在全局配置中设置。

页面操作：

1. 进入「全局配置」，源目录应为 `/files/file_server`，备份目录为 `/mnt/files`。云端填 IP `10.90.0.4`、路径 `/files/file_server`、用户 `root`、密码及端口 `22`，保存配置。本地目录由 Compose 配置，页面只读；数据库放在 `data` 里。
2. 尚未备份：先「预览备份差异」，再「开始备份」。已有磁盘备份：直接「开始扫描」，相同文件记为已备份，新增或变化文件记为增量。
3. 云端已有全量文件后，点击「测试连接」，成功后「开始上传」。上传前会重新扫描，传输新增、变化及需要重试的条目。
4. 「同步增量记录」显示第 1 次、第 2 次等上传清单、结果与大小折线图。大小按选中的普通文件完整大小合计，目录不计大小。

启动页面、保存配置都不会自动执行任务。密码仅保存在当前浏览器会话对应的服务内存中，服务重启后重新输入。

`data` 还保存页面配置和任务日志；更新部署时保留整个目录。数据库在首次实际备份或扫描后创建。`data` 和 `ssh` 都位于项目目录下，不依赖项目安装位置。

账本使用 WAL 模式，扫描写入时页面可以读取上次已提交的记录，扫描提交后再更新统计。旧账本在新版工作台启动或脚本首次打开时自动切换，保留已有基准和上传历史，无需重建；没有账本时仍由首次任务创建。

`data` 应放在源服务器本地磁盘。SQLite 自动管理 `backup_state.db-wal`、`backup_state.db-shm`，它们随整个 `data` 目录挂载；运行中不要删除，也不要只复制 `.db` 作为完整备份。备份账本使用 SQLite 的 `backup()`，或停止工作台及其他账本进程后再复制。备份文件目录 `/mnt/files` 仍可使用 NFS。

### 使用 venv 直接启动（不使用 Docker）

需要 Python 3.9+，先安装第 2 节的系统依赖。在项目根目录创建并激活虚拟环境，再安装依赖、启动服务：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
VIEW_SOURCE_PATH=/files/file_server VIEW_DISK_PATH=/mnt/files python app.py --host 0.0.0.0 --port 5080
```

以后启动时不用重新创建 `.venv`，先执行 `source .venv/bin/activate`，再执行上面的启动命令。端口修改 `--port` 即可。按 `Ctrl+C` 停止服务，执行 `deactivate` 退出虚拟环境。

Ubuntu / Debian 如果创建虚拟环境时提示缺少 `ensurepip`，先安装 `python3-venv`：

```bash
apt-get install -y python3-venv
```

`requirements.txt` 包含 Flask 和 Linux x86_64 CPython 的 SQLite 备用驱动。内置 SQLite 缺失或版本低于 3.22 时自动使用备用驱动，兼容旧 CentOS 的 WAL 只读访问。Flask 所需的 Werkzeug、Jinja2 等会自动安装；`json`、`os` 等标准库不用额外安装。Dockerfile 已安装 rsync、SSH、sshpass 和 CA 证书，NFS 挂载在宿主机完成。

## 2. 不使用 Flask：独立运行脚本

在 `10.90.0.2` 上运行，只需 `ScanSyncBackup/scan_sync_backup.py`、Python 3.9+（支持 SQLite）及传输工具，不需要安装 Flask。建议在 Linux 上以 root 运行，以保留文件权限、属主等信息。

```bash
# CentOS / RHEL（sshpass 如不可用，需要先启用对应版本的 EPEL 源）
yum install -y rsync openssh-clients sshpass
# Ubuntu / Debian
apt-get install -y rsync openssh-client sshpass

python3 --version
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
python3 scan_sync_backup.py --help
```

Linux x86_64 CPython 如果缺少 `_sqlite3` 或内置 SQLite 版本低于 3.22，安装备用驱动后重新运行：

```bash
python3 -m pip install 'pysqlite3-binary>=0.5.4,<0.6'
```

以下命令已带上本次源目录和备份目录，账本保存在 `data/backup_state.db`。执行前先挂好 NFS。

```bash
# 第一次备份：先预览，再实际复制并保存磁盘基准
python3 scan_sync_backup.py backup --source /files/file_server --disk /mnt/files --dry-run
python3 scan_sync_backup.py backup --source /files/file_server --disk /mnt/files

# 已有磁盘备份但没有数据库：直接扫描，自动读取磁盘基准并对比源目录
# 有基准后只扫描源目录，不再需要连接备份磁盘
python3 scan_sync_backup.py scan --source /files/file_server --disk /mnt/files

# 查看当前状态
python3 scan_sync_backup.py status
```

独立脚本使用密码上传时，在脚本顶部配置 `CLOUD_PASSWORD`（该值保存在脚本文件中）。先在运行脚本的机器上执行一次 `ssh -p 22 root@10.90.0.4`，输入 `yes` 确认指纹并登录，成功后 `exit` 退出，再执行：

```bash
# 上传增量；云端需已安装 rsync，并允许写入目标目录
python3 scan_sync_backup.py upload \
  --source /files/file_server --disk /mnt/files \
  --cloud-host 10.90.0.4 --cloud-user root --cloud-path /files/file_server \
  --ssh-port 22
```

脚本没有 `--password` 参数，仅设置 `SSHPASS` 不会自动启用密码认证。已有可用密钥时，也可通过 `--ssh-key /实际私钥路径` 指定。

保留 `data/backup_state.db`，不要同时从脚本与页面操作同一账本。扫描只读取文件；备份与上传不删除目标已有文件。增量根据类型、大小、修改时间（1 秒容差）、权限、属主及链接目标判断；扩展属性（包括 SELinux 标签和 ACL）不参与差异判断，不做完整内容哈希，也不重新核验云端文件。

## 3. NFS：把硬盘服务器的硬盘目录挂到源服务器

### 第一步：在 10.90.0.3 上共享硬盘目录

硬盘文件已经在 `/data/file_server`，直接共享这个目录，不需要重新挂硬盘。

```bash
yum install -y nfs-utils
ls /data/file_server
```

打开 `/etc/exports`，加上这一行：

```text
/data/file_server 10.90.0.2(rw,sync,no_subtree_check,no_root_squash)
```

意思是只允许源服务器读写这个目录；允许 root 保留备份文件的属主。

```bash
systemctl enable --now nfs-server
exportfs -rav
exportfs -v
```

硬盘服务器如果开了 firewalld，再执行下面两条，放行 NFS：

```bash
firewall-cmd --permanent --add-service=nfs
firewall-cmd --reload
```

### 第二步：在 10.90.0.2 上挂载

安装 NFS 客户端，按源服务器的系统选一条：

```bash
# CentOS / RHEL
yum install -y nfs-utils
# Ubuntu / Debian
apt-get install -y nfs-common
```

把硬盘服务器的共享目录挂到 `/mnt/files`：

```bash
mkdir -p /mnt/files
mount -t nfs -o vers=4.1 10.90.0.3:/data/file_server /mnt/files
findmnt /mnt/files
ls /mnt/files
```

现在，源服务器上的 `/mnt/files/a.txt` 就是硬盘服务器上的 `/data/file_server/a.txt`。源文件 `/files/file_server/a.txt` 也应该对应这一份备份，不要再多套一层 `file_server` 目录。

需要开机自动挂载，就在源服务器的 `/etc/fstab` 加这一行：

```text
10.90.0.3:/data/file_server /mnt/files nfs defaults,vers=4.1,_netdev,nofail 0 0
```

备份或启动容器前，执行 `mountpoint /mnt/files`，显示已挂载再继续；没挂上时不要备份，否则会写到源服务器的本地磁盘。开机自动挂载失败也要先处理好再启动容器。

重新挂载 NFS 后，先结束正在执行的任务，再在项目根目录执行：

```bash
docker-compose up -d --force-recreate
```

已有硬盘备份、不想重新复制：挂好后直接点「开始扫描」，脚本会读取 `/mnt/files`，与 `/files/file_server` 对比并保存账本。已有基准后，后续扫描只读源目录。

出现 `Operation not supported` 时，通常是 NFS 不支持脚本要保留的 ACL 或扩展属性，需要处理后再确认备份成功。云端也需要安装 rsync，并允许写入 `/files/file_server`。

升级为忽略扩展属性的版本后，保留原数据库，再执行一次「开始扫描」即可重新分类，无需重新读取硬盘基准或清空上传历史。仅扩展属性变化不会触发上传，rsync 传输时仍使用原来的属性保留参数。
