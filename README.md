# Marzban Panel

基于 [Marzban v0.8.4](https://github.com/Gozargah/Marzban/tree/v0.8.4) 的代理管理面板，提供用户入站分配、节点接入和面板迁移。

[操作指南](docs/operations-guide.md) · [节点安装](docs/node-installer.md) · [迁移说明](docs/migration.md)

## 功能

- 入站由管理员手动分配，新增入站和新用户默认不勾选；支持空选择并保留已有分配。
- 出口支持 VLESS + TCP + REALITY 和 Cloudflare WebSocket + TLS。
- 全新部署与迁移独立；迁移先隔离验收，确认后停机切换。

## 全新部署

在新主控服务器的 root 终端执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.2.1/install.sh)
```

按提示填写面板域名、订阅域名和 Cloudflare 信息。完成后访问 `https://面板域名/dashboard/`。

## 新增出口

在新出口服务器的 root 终端执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.2.1/node.sh)
```

安装完成后，在面板为用户勾选需要的入站。

## 迁移旧面板

在新主控服务器的 root 终端执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.2.1/migrate.sh)
```

默认停在隔离验收阶段，旧面板继续运行；正式切换需要单独确认。

## 环境与镜像

支持 Debian 12/13、Ubuntu 22.04/24.04/26.04，要求 amd64、公网 IPv4 和可用 TCP 443。完整准备与填写示例见[操作指南](docs/operations-guide.md)。

镜像：`ghcr.io/taozi-succes/marzban-panel:v1.2.1`。

本项目遵循 [AGPL-3.0](LICENSE)。上游资料见[原版 README](README.upstream.md)。
