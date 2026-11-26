# Marzban Panel

基于 [Gozargah/Marzban v0.8.4](https://github.com/Gozargah/Marzban/tree/v0.8.4) 的通用面板，保留 AGPL-3.0 许可证。源码、安装脚本和 GHCR 镜像公开；安装不需要 GitHub Token。

## 定制行为

- 新增入站默认不分配给现有用户；新建用户默认没有任何入站。
- 支持取消全部入站后保存，原有明确授权继续保留。
- 修复 REALITY shortId 订阅生成和新版 Xray X25519 输出兼容。
- 不包含链式代理功能。

运行时的服务器地址、管理员密码、Cloudflare Token、数据库和节点密钥由操作者在部署时提供，不写入仓库或镜像。本仓库从经过清理的源码建立独立历史，不包含任何旧部署历史或服务器备份。

## 全新部署

适用于 Debian 12/13、Ubuntu 22.04/24.04/26.04 的 amd64 专用主控服务器，要求 root、SSH、可用 TCP 443 和公网 IPv4。请先安装 curl（如果系统未预装）。安装器会检查并安装 Docker Compose、Certbot 等依赖。

```bash
sudo -i
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.0.0/install.sh)
```

交互填写：面板完整域名、订阅完整域名、Cloudflare 区域、当前服务器 IPv4、证书邮箱、镜像版本、新管理员账号／密码、该区域的 DNS 编辑 Token。域名均填写自己的值，例如 `panel.example.com`、`sub.example.com` 和区域 `example.com`。镜像版本直接回车；不需要登录 GitHub 或 GHCR。

Cloudflare Token 只需要目标区域的 DNS 编辑权限。安装器创建两条橙云 A 记录，通过 DNS-01 签发双域名 Let's Encrypt 证书并配置自动续期。若 DNS 已指向其他服务器，会停止以免覆盖现有业务。请在 Cloudflare 选择 **Full (strict)** SSL 模式，并确保这两个域名不受额外 Access 登录或交互式验证码规则拦截。

完成后访问 `https://面板域名/dashboard/`。这会创建独立空面板，**没有旧用户、旧节点，也不会迁移旧管理员**。初始核心仅含容器内 loopback SOCKS 占位监听，不发布任何用户代理入口。随后使用公开的 [节点安装器](https://github.com/Taozi-succes/marzban-node-installer) 添加出口，在面板手动分配用户入站。

安装器默认目录 `/opt/marzban-panel`。重复执行只继续未完成的安装，不重置已完成的面板。可先加 `--plan` 查看计划。

```bash
marzban-panel status
```

管理面板和订阅使用不同域名；订阅域名仅开放订阅路径。后端 HTTP 只绑定本机，443 由 Nginx 提供；系统防火墙保留 SSH，并限制 443 来源到 Cloudflare。Nginx 同时执行来源限制，避免依赖 Docker 发布端口的 UFW 行为。云厂商安全组仍由操作者放行 SSH、TCP 443。

## 独立迁移工具

**全新部署不会触发迁移。** 迁移另用 `migrate.sh`／`marzban-migrate`，操作顺序和停机边界见 [迁移说明](docs/migration.md)。当前只接受同一套定制 v0.8.4、SQLite 和匹配的应用指纹；不隐式升级官方原版或不同版本数据库。

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.0.0/migrate.sh) --help
```

`backup` 在线只读导出，供预演；`freeze` 在确认后停止源主控并导出最终包；`restore` 进入禁止写入、订阅和节点连接的隔离验收模式；`activate` 要求最终停机快照并再次明确确认唯一主控接管。预演包不能激活。真实备份使用口令加密，不能提交到 GitHub。

## 本机文件

| 路径 | 内容 |
| --- | --- |
| `/opt/marzban-panel/data/` | SQLite、核心和迁移后的应用文件 |
| `/opt/marzban-panel/.env` | 运行环境，仅 root 可读写 |
| `/opt/marzban-panel/credentials/cloudflare.ini` | DNS Token，权限 600 |
| `/opt/marzban-panel/state.json` | 部署状态，仅 root 可读写 |
| `/opt/marzban-panel/releases/` | 目标恢复前的本地回退数据 |
| `/etc/letsencrypt/live/面板域名/` | 自动续期证书和私钥 |

管理员密码只经终端隐藏输入并由应用存为数据库密码哈希，不写入命令参数或部署状态。迁移保留原管理员哈希、用户 UUID、JWT 和节点 TLS 身份。

## 镜像与验证

公开镜像：`ghcr.io/taozi-succes/marzban-panel:v1.0.0`。部署会记录实际拉取的不可变 sha256 摘要；不使用 `latest`。发布流程先构建和测试，通过后才推送镜像。

```bash
docker build -t marzban-panel:test .
docker run --rm --network none -v "$PWD:/repo:ro" -w /repo --entrypoint python marzban-panel:test -m unittest discover -s tests/operations -v
python3 tests/operations/integration.py --image marzban-panel:test
```

CI 另外运行旧入站授权迁移回归。集成测试只使用独立 Docker 容器、卷和隔离网络，验证空面板、新用户默认授权、加密导入、完整数据校验、隔离验收和最终恢复。测试不会联系正式面板、修改真实 DNS 或签发真实证书；实际云主机部署和证书签发仍需在操作者提供的新服务器验收。

上游功能与开发资料见 [上游 README](README.upstream.md)。公开源码遵循 [LICENSE](LICENSE)。
