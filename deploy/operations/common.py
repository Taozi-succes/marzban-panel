"""Local operator utilities; never persist passwords or print subprocess output."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

VERSION = "1.2.1"
DEFAULT_IMAGE = "ghcr.io/taozi-succes/marzban-panel:v1.2.1"
NGINX_IMAGE = "nginx@sha256:901e593c95dc85f5a729f5765516321366ef4ac401821431d408897e95287d0a"
MAX_BUNDLE = 256 * 1024 * 1024


@contextmanager
def operator_lock():
    """Serialize installs and cutovers on this host, including source exports."""
    import fcntl
    with open('/var/lock/marzban-panel-operator.lock', 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolError('另一个面板部署或迁移命令正在运行') from None
        yield


class ToolError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def private_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ToolError("配置文件不能是符号链接")
    data = data.encode() if isinstance(data, str) else data
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".next-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, data):
    private_write(path, json.dumps(data, indent=2, ensure_ascii=False)+"\n")


def run(args, data=None, timeout=900, visible=False,error_prefix=None):
    done = threading.Event()
    started = time.monotonic()
    def beat():
        while not done.wait(15):
            print(f"  [等待] {Path(args[0]).name} {int(time.monotonic()-started)} 秒", flush=True)
    worker = threading.Thread(target=beat, daemon=True)
    worker.start()
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
        if result.returncode:
            if error_prefix:
                for line in result.stderr.decode(errors='replace').splitlines():
                    if line.startswith(error_prefix):raise ToolError('源端迁移停止：'+line[len(error_prefix):][:300])
            raise ToolError(f"{Path(args[0]).name} 执行失败，退出码 {result.returncode}；输出未公开以保护凭据")
        if visible:
            print(result.stdout.decode(errors="replace"))
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise ToolError(f"{Path(args[0]).name} 无法执行或超时") from None
    finally:
        done.set()
        worker.join(timeout=1)


def root_path(value):
    path = Path(value)
    if not re.fullmatch(r"/opt/[A-Za-z0-9_-]+", str(path)) or path.is_symlink():
        raise ToolError("部署目录必须是 /opt 下不含空格的独立目录")
    return path


def hostname(value):
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}", value) or len(value)>253:
        raise ToolError("请填写小写完整域名")
    return value


def validate_config(config):
    config = dict(config)
    for key in ("panel_domain", "sub_domain", "zone"):
        hostname(config[key])
    if config["panel_domain"] == config["sub_domain"]:
        raise ToolError("面板和订阅请使用不同域名")
    if any(not config[k].endswith("."+config["zone"]) for k in ("panel_domain", "sub_domain")):
        raise ToolError("两个域名必须属于指定 Cloudflare 区域")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config["email"]):
        raise ToolError("证书邮箱无效")
    if not re.fullmatch(r"(?:ghcr\.io/[a-z0-9_./-]+)(?:@sha256:[a-f0-9]{64}|:v\d+\.\d+\.\d+(?:[a-z0-9_.-]*)?)", config["image"]):
        raise ToolError("镜像必须是 GHCR 固定版本或 sha256 摘要，不接受 latest")
    if not 1024 <= int(config.get("http_port", 18080)) <= 65535:
        raise ToolError("本机 HTTP 端口无效")
    return config


def render_env(values):
    # Compose uses env_file.format=raw: preserve dollar signs and quotes literally.
    lines=[]
    for key,value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ToolError("环境变量名称无效")
        value=str(value)
        if any(c in value for c in ('\n','\r','\x00')):
            raise ToolError("环境变量含不支持的换行，需要先人工整理")
        lines.append(key+'='+value)
    return "\n".join(lines)+"\n"


def parse_env(path):
    result={}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key,sep,value=line.partition('=')
        if not sep:
            raise ToolError("环境文件格式无效")
        result[key.strip()]=value
    return result
