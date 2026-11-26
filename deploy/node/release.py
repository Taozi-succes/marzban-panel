"""Download a fixed public installer release without GitHub credentials."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from api import InstallError

REPOSITORY = "Taozi-succes/marzban-panel"
FILES = {"installer.py", "api.py", "plan.py", "system.py", "probe.py", "release.py", "install.sh", "SHA256SUMS"}


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in ("api.github.com", "codeload.github.com"):
            raise InstallError("GitHub 下载重定向地址不符合预期")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if target.hostname != urlsplit(req.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def extract_bundle(data, directory):
    found = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for entry in archive:
            parts = PurePosixPath(entry.name).parts
            if ".." in parts or PurePosixPath(entry.name).is_absolute():
                raise InstallError("安装包路径不正确")
            if len(parts) != 4 or parts[1:3] != ("deploy", "node") or parts[-1] not in FILES:
                continue
            if not entry.isfile() or entry.size > 1024 * 1024 or parts[-1] in found:
                raise InstallError("安装包包含重复或不支持的文件")
            path = Path(directory)/parts[-1]
            path.write_bytes(archive.extractfile(entry).read())
            path.chmod(0o600)
            found.add(parts[-1])
    if found != FILES:
        raise InstallError("安装包缺少必要文件")
    expected = {}
    for line in (Path(directory)/"SHA256SUMS").read_text().splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise InstallError("安装包校验清单不正确")
        digest, name = fields
        if name in expected or name not in FILES - {"SHA256SUMS"} or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise InstallError("安装包校验清单不正确")
        expected[name] = digest
    if set(expected) != FILES - {"SHA256SUMS"}:
        raise InstallError("安装包校验清单不完整")
    if any(hashlib.sha256((Path(directory)/name).read_bytes()).hexdigest() != digest for name, digest in expected.items()):
        raise InstallError("安装包校验失败")


def download(tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise InstallError("升级版本格式应为 v1.0.0")
    req = Request(f"https://codeload.github.com/{REPOSITORY}/tar.gz/refs/tags/{tag}",
                  headers={"User-Agent": "marzban-node-installer"})
    try:
        with build_opener(GitHubRedirect(), HTTPSHandler()).open(req, timeout=60) as response:
            data = response.read(64 * 1024 * 1024 + 1)
    except (OSError, URLError):
        raise InstallError("公开安装包下载失败，请检查网络和版本标签") from None
    if len(data) > 64 * 1024 * 1024:
        raise InstallError("安装包超出预期大小")
    return data


def upgrade(tag, root):
    data = download(tag)
    with tempfile.TemporaryDirectory(prefix="marzban-node-release-") as directory:
        extract_bundle(data, directory)
        result = subprocess.run(["bash", str(Path(directory)/"install.sh"), "--root", str(root), "reconfigure"])
        if result.returncode:
            raise InstallError("升级尚未完成，已有版本和状态记录可继续用于恢复")
