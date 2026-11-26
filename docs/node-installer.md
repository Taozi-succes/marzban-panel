# Marzban Node Installer

在新服务器交互式安装标准 Marzban-node，提供 **VLESS + TCP + REALITY** 和 **Cloudflare + WebSocket + TLS**，接入已有面板。安装和升级均匿名下载，不需要 GitHub Token。

节点安装器与主面板在 `Taozi-succes/monster_panel` 统一维护。运行脚本位于 `deploy/node/`，测试位于 `tests/node/`；仓库不包含生产部署资料、真实凭据或用户数据。

`v1.2.0` 将安装、匿名升级、文档和测试统一到本仓库。Cloudflare 网段通过官方公开 API `https://api.cloudflare.com/client/v4/ips` 获取；网段未取得或校验失败时不改防火墙。安装会显示阶段，长命令每 15 秒显示等待时间，不输出密码或命令中的秘密参数。管理 CLI 在安装依赖前保存，后续失败也可查看状态。

## 一键安装

在新服务器的 **root SSH 终端**执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/node.sh)
```

非 root 先运行 `sudo -i`；缺少 curl 时先执行 `apt-get update && apt-get install -y curl ca-certificates`。入口固定下载 `v1.2.0`，仅提取允许的安装文件，核对 SHA256 后运行，退出后清理临时目录。来源信任依赖 HTTPS 和发布者，包内校验和用于检测损坏。

只预览计划：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/node.sh) --plan
```

预览读取面板和校验 Cloudflare 区域，不写 DNS、证书、用户或节点配置。入口可能在执行命令的新服务器上安装缺少的 Python/OpenSSL 工具。

支持 Debian 12/13、Ubuntu 22.04/24.04/26.04，**amd64、systemd、公网 IPv4**。ARM 和 IPv6 入口暂不支持。适合全新系统，遇到未知部署目录、节点重名或端口冲突会停止。

## 依次填写

| 提示 | 填什么 |
| --- | --- |
| 面板 HTTPS 地址 | 已有面板根地址，如 `https://panel.example.com`，不带 `/dashboard/#/` |
| 面板服务器公网 IPv4 | 管理来源的真实源站 IP，不填 CDN 边缘 IP |
| 节点标识、名称 | 如 `us04`、`美国 04`，标识保持唯一 |
| 本节点公网 IPv4 | 确认自动检测的是新服务器；当前检测要求出口 IPv4 与之相同 |
| 模式 | 默认 `both`；或仅 `reality`、`ws` |
| REALITY 目标域名 | 可改默认值，需服务端能连接其 TCP 443，订阅 SNI 与之相同 |
| Cloudflare 区域、WS 域名 | 如 `example.com`、`us04-ws.example.com`，每台服务器独立域名 |
| 证书邮箱 | Let's Encrypt 账号及通知邮箱 |
| 管理端口、API 端口 | 默认 TCP 62050、62051 |
| REALITY、WS 本地端口 | 回车自动避开全局入站和本机监听端口 |
| 面板管理员账号、密码 | 需要 sudo 管理员；密码隐藏输入，登录 Token 仅存在内存 |
| Cloudflare DNS 编辑 Token | 仅指定区域 `Zone → DNS → Edit`，隐藏输入 |

最后核对计划，输入 **yes** 才安装、注册和发布。Cloudflare 区域需已激活、TLS 模式为 **Full (strict)**；DNS Token 不更改区域 TLS 策略。自动创建橙云 A 记录、DNS-01 签发证书和配置续期，不需要开放 80。同名 DNS 仅在完全匹配时复用；不同业务记录不会覆盖。仅安装器自建记录在重配时跟随 IP 更新。

默认目录 `/opt/marzban-exit`。Cloudflare Token 单独保存在 `credentials/cloudflare.ini`（root、600）供续期，Nginx 不挂载该文件；面板密码不保存。状态和 REALITY 私钥存于安装目录（目录 700、文件 600），不要公开分发包含这些文件的快照。

## 面板与用户

主控需要支持**显式入站分配**：新增入站不给现有用户、新用户允许空选择。未修改的上游 Marzban v0.8.4 会自动授权新入站；安装器检测到后撤回并停止。脚本不修改主面板源码或权限策略。

通过标准 API 注册节点（`add_as_new_host=false`），增加 `<标识>-reality` / `<标识>-ws` 和对应 Host，保留原线路、用户选择、到期时间、配额与已用流量。成功后在面板用户编辑页主动勾选线路，再更新订阅。不包含链式代理。

**发布核心配置会向所有连接节点下发配置，并短暂重启 Xray。** 安装在新机运行，主控仍需新增配置。多台安装串行执行，期间避免人工编辑核心：全量 PUT 没有原子条件更新，提交前比较不能消除检查后的并发写入窗口。

## 防火墙与日常管理

云安全组需手动放行：保留 SSH；REALITY 端口允许用户来源；443 允许 Cloudflare IPv4；管理/API 端口仅允许面板公网 IPv4。云侧 443 也可放行公网，由系统 UFW 限制 Cloudflare。实际端口以计划为准。

系统 UFW 保留其他业务规则及本次 SSH 服务端端口，只重建自身标记规则。WS 后端仅监听回环。容器采用 host 网络，避免端口映射绕过 UFW。Cloudflare 网段在安装或重配时更新。

```bash
marzban-node status
marzban-node check
marzban-node reconfigure --plan
marzban-node reconfigure
marzban-node upgrade --release v1.2.0
marzban-node rollback
```

中断后重跑一键命令继续保存的计划，CLI 已安装后也可用 `marzban-node install`。核心发生变化时先 `reconfigure --plan`，再重配。重复安装复用自身节点、密钥、标签和 DNS。旧版迁移：运行公开一键命令，保持原目录、标识、面板、模式和端口，可沿用已有状态并更新 CLI，无需 GitHub Token。

重配可改显示名称、公网地址、管理来源、WS 域名和邮箱；旧域名 DNS/证书不自动删除。协议或端口迁移单独安排。升级匿名下载固定标签、校验后进入重配，镜像 digest 随验收版本固定。

`rollback` 只撤回最近记录的面板入站和 Host 变更，保留其他核心修改和用户计量；人工修改过的对象会停止回退。它不卸载容器、不删 DNS/证书、不回退本机 Nginx/UFW。历史计划在 `releases/`。

`check` 使用 15 分钟过期、10 MiB 配额的临时账号验证双协议、出口和用户/节点计量，再删除账号；清理未确认会报错并保留恢复记录。WS 走公网域名，REALITY 从回环认证，公网入口仍需外部客户端验证。`marzban-node-renew.timer` 每日检查两次续期，成功后重载 Nginx。

## 测试与发布

### 交互切换迁移后的主面板

主面板迁到新服务器／域名，并保留原数据库和节点身份后，在每台出口执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/node.sh) switch-panel
```

填写新面板 HTTPS 地址、新主控公网 IPv4 和管理员账号／密码；可在末尾加 `--plan` 只检查。脚本核对节点 TLS 身份、节点编号、地址、端口和原入站。确认旧主控已停止且云安全组已放行新来源后，只更新本地安装记录和两个管理端口的 UFW 来源规则，保留代理端口、密钥、WS 域名、证书和用户授权；不发布核心配置，不重启代理容器。会先保存原状态到 `releases/panel-switch-*.json`。

连接检查失败时，已保存的新地址可继续使用 `switch-panel` 重试或 `check` 排查。不会自动恢复旧主控。完成后已安装的 CLI 可直接执行 `marzban-node switch-panel`。尚在旧版本的 CLI 先用上面的公开命令，无需 GitHub Token。

独立空面板不含原节点身份和用户，不能用此命令直接接管；需要单独导入数据或建立独立节点实例。

离线回归覆盖冲突保护、幂等恢复、用户数据保留、默认权限、回退、私有文件权限、匿名升级、包校验和文档的一键命令。

容器集成仅使用**公开上游** Marzban、标准节点、Nginx 和显式信任测试 CA 的 HTTPS 服务：验证上游自动授权被拒绝并撤回，然后在空账号测试库验证双协议、实际计量、清理和回退。公开 CI 不包含私有主面板源码；显式分配契约由 HTTP 夹具覆盖。

开发和 CI 不连接正式服务器、不调用真实 Cloudflare DNS、不签真实证书。完整云主机 apt/systemd/UFW、ACME 和 Cloudflare 边缘路径仍需首台测试机验收。CI 只测试，不部署服务器。

维护时执行 `python3 -m unittest discover -s tests/node -v`（Linux、OpenSSL），Docker 集成命令见 `.github/workflows/validate-node.yml`。更新运行代码后在 `deploy/node` 执行：

```bash
sha256sum installer.py api.py plan.py system.py probe.py release.py install.sh > SHA256SUMS
```

发布新标签时同步更新入口固定版本及本文一键命令。
