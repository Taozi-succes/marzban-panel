# monster_panel 操作指南

适用版本：面板与节点安装器 `v1.2.0`。以下命令由你在对应服务器执行。

- **全新面板，只建新用户**：做第 1、2、3 节。
- **搬迁旧面板，保留用户和流量**：做第 1、4 节，验收后再做第 5 节。
- **只增加出口节点**：直接做第 3 节。

## 1. 准备

准备 Debian 12 x86_64 服务器，使用 root 登录。已有面板的旧服务器暂时保持运行。

| 项目 | 本文示例／填写要求 |
| --- | --- |
| 管理域名 | `panel.taozi.ccwu.cc` |
| 订阅域名 | `sub.taozi.ccwu.cc` |
| Cloudflare 区域 | `taozi.ccwu.cc` |
| 新面板 IP | 新主控服务器的真实公网 IPv4 |
| 新出口 IP | 出口服务器自己的真实公网 IPv4 |
| Cloudflare Token | 仅限该区域的「区域 → DNS → 编辑」权限；有效的原 Token 可以复用 |

不需要 GitHub Token。密码和 Cloudflare Token 在安装交互中输入；Token 只粘贴值，不带配置文件前缀。

**DNS 和 HTTPS：**

1. Cloudflare 添加 `panel`、`sub` 两条 A 记录，均指向**新主控 IP**，开启橙云代理。
2. SSL/TLS 加密模式选 **完全（严格）**。订阅和 API 路径不要启用交互式人机验证。
3. 出口 WS 域名如 `us04-ws.taozi.ccwu.cc` 指向**该出口 IP**，开启橙云；安装器可创建缺失的记录，已有错误记录需自行改正。

**云防火墙放行：**

| 服务器 | 入站 TCP 端口 |
| --- | --- |
| 新主控 | SSH 实际端口、`443` |
| 出口 | SSH 实际端口、`443`、安装计划显示的 REALITY 端口 |
| 出口的管理端口 | 默认 `62050`、`62051`，仅允许**主控真实公网 IP**访问 |

DNS 验证签证书不要求开放 `80`；WS 本地端口和面板内部端口不用对公网开放。主控与出口按独立服务器操作。

若没有 curl，先执行：

```bash
apt-get update
apt-get install -y curl ca-certificates
```

## 2. 全新部署主面板

在**新主控服务器**执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/install.sh)
```

| 交互选项 | 怎么填 |
| --- | --- |
| 管理面板完整域名 | `panel.taozi.ccwu.cc` |
| 订阅完整域名 | `sub.taozi.ccwu.cc` |
| Cloudflare 区域 | `taozi.ccwu.cc` |
| 本服务器公网 IPv4 | 核对为新主控真实 IP；正确就回车 |
| 证书通知邮箱 | 你的邮箱 |
| 公开镜像版本 | 回车使用默认值 |
| 新管理员账号 | `admin` 或自定义 |
| 新管理员密码 | 自设，至少 12 位；输入不显示 |
| Cloudflare DNS 编辑 Token | 粘贴 Token；仅本机已有有效凭据时可回车沿用 |
| 确认全新部署 | 核对后输入 `yes` |

完成后执行：

```bash
marzban-panel status
```

看到 `mode: fresh`、`phase: active` 后，打开 <https://panel.taozi.ccwu.cc/dashboard/>，用刚设置的账号登录。此时是空面板，下一步添加出口和用户。

## 3. 新增一个出口节点，并分配给用户

在**全新出口服务器**执行；已有业务节点不要按首次安装处理。

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/node.sh)
```

| 交互选项 | 怎么填 |
| --- | --- |
| 面板 HTTPS 地址 | `https://panel.taozi.ccwu.cc`，不带 `/dashboard/` |
| 面板管理服务器公网 IPv4 | 当前主控真实公网 IP |
| 节点标识 | `us04`；每台唯一，下台用 `us05` 等 |
| 节点显示名称 | `美国 04`，按实际地区填写 |
| 本节点入站公网 IPv4 | 核对为这台出口自己的 IP |
| 协议模式 | 回车选 `both`，同时安装 REALITY 和 WS + TLS |
| REALITY 目标域名 | 回车使用 `www.cloudflare.com` |
| Cloudflare 区域 | `taozi.ccwu.cc` |
| WS 完整域名 | `us04-ws.taozi.ccwu.cc`，每台不同 |
| 证书通知邮箱 | 你的邮箱 |
| 节点管理端口 | 回车，默认 `62050` |
| Xray API 端口 | 回车，默认 `62051` |
| REALITY 端口 | 回车自动分配，记下安装计划显示的端口 |
| WS 本地端口 | 回车自动分配，不必公网放行 |
| 面板管理员账号、密码 | 当前面板有管理权限的管理员；密码输入不显示 |
| Cloudflare DNS 编辑 Token | 粘贴 Token；本机已保存有效凭据才可回车 |
| 确认安装并发布 | 先放行计划中的端口，再输入 `yes` |

一次安装一台。发布核心配置可能使已有节点连接短暂重连。

安装结束后：

```bash
marzban-node status
marzban-node check
```

`status` 应为 `phase: complete`。`check` 会使用临时测试用户检查代理和计量，结束后清理该用户。

然后到面板操作：

1. 在节点列表确认新节点在线。
2. 创建或编辑用户，手动勾选 `us04-reality`、`us04-ws` 等需要分配的入站，保存。
3. 复制该用户的完整订阅链接，导入客户端或更新订阅。
4. 客户端分别测试两种线路，查看出口 IP；产生一些流量后检查面板用量是否增长。

**新增入站和新用户默认不勾选线路；取消全部入站也能保存。**已有用户已选线路保留。服务器自检通过后，仍需用实际客户端验证公网连接。

## 4. 迁移旧面板：先备份并隔离验收

在**新主控服务器**执行。无需先单独部署空面板；没有目标面板时，向导会先引导安装，填写方法见第 2 节。

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/migrate.sh)
```

目标面板应为空，不要提前增加业务用户或节点。

| 交互选项 | 怎么填 |
| --- | --- |
| 迁移菜单 | 选 `1`：在线备份并隔离恢复 |
| 旧服务器 SSH 地址 | 旧主控 IP 或 SSH 域名，不带 `https://`、不带 `用户@` |
| SSH 端口 | 实际端口，通常回车选 `22` |
| SSH 用户 | `root`，或实际拥有 Docker 操作权限的账号 |
| SSH 认证方式 | 按实际选 Agent、私钥文件或密码 |
| 私钥路径（选私钥时） | **新主控上**的 Linux 绝对路径，权限 `600`；不能填 Windows 本地路径 |
| 源端 Docker 是否需要免密 sudo | root 或 Docker 组用户选 `no`；需要 `sudo -n docker` 的账号选 `yes` |
| 旧主控公网 IPv4 | 旧主控真实公网 IP |
| 旧面板完整域名 | `vpn.itao.online`，按实际填写，不带协议 |
| 备份加密口令 | 自设至少 12 位，重复输入并妥善保存；续跑和正式切换还要用 |
| 确认连接并迁移 | 核对源服务器、目标域名后输入 `migrate` |
| SSH 主机指纹／认证 | 首次连接核对指纹，再按提示认证 |
| 源容器 | 按列表选择实际 Marzban 容器，记下名称 |

旧服务器需允许新主控通过 SSH 连接，所选 SSH 用户需已有 Docker 权限。

完成后在新主控执行：

```bash
marzban-panel status
marzban-migrate verify
```

打开新面板，用**旧面板管理员账号密码**登录，核对用户、到期时间、流量额度、已用流量和入站分配。

此时应为 `mode: migration`、`phase: staging`。**节点禁用、订阅返回 503、业务修改被拒绝均属于隔离状态的预期行为。旧面板继续运行。**

**验收完可停在这里。需要正式切换时，才执行下一节。**

## 5. 验收后正式切换

1. 提前允许新主控 IP 访问各出口管理端口；核对云防火墙和系统防火墙，准备期间保留旧主控来源。
2. 如果旧主控本身还承担用户出口，先单独处理这些线路；向导不会自动搬迁主控本机出口，存在已授权的本机出口时会阻止停机。
3. 在新主控重新执行第 4 节迁移命令，选 `2`「正式停机切换」，填写原 SSH 信息和备份口令。
4. 向导最终要求输入 `cutover 实际源容器名`。仅确认允许旧面板停机后输入；例如实际容器叫 `marzban`，才输入 `cutover marzban`。

确认后脚本会停旧面板、取得最终备份、恢复并激活新面板。旧面板保持停止，不做旧域名转发。

### 现有出口切换到新主控

等新面板 `phase: active`、旧面板已停止，再在**每台由本安装器完成安装的出口**执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/monster_panel/v1.2.0/node.sh) switch-panel
```

| 交互选项 | 怎么填 |
| --- | --- |
| 新面板 HTTPS 地址 | `https://panel.taozi.ccwu.cc` |
| 新面板公网 IPv4 | 新主控真实 IP |
| 管理员账号、密码 | 迁移后保留的旧管理员账号密码 |
| 最终确认 | 核对后输入 `switch` |

早期手动部署、没有安装器记录的节点，按原部署方式调整管理来源；不要在这些节点上直接运行首次安装命令。

### 最后验收

- 新面板节点在线，用户、到期时间、额度、已用流量和入站分配正确。
- 每台出口的 REALITY、WS 客户端连接正常，出口 IP 正确，流量统计增长。
- 从新面板复制每位用户的新订阅链接，通知用户重新导入；使用 `sub.taozi.ccwu.cc` 的完整用户专属链接。
- 测通后再移除出口防火墙中不再需要的旧主控来源。

若正式切换在旧面板停机后失败，先查看状态，再运行原向导选 `2` 续跑；不要自行让两套主控同时接管节点。

## 6. 查看状态与日志

新主控：

```bash
marzban-panel status
cd /opt/marzban-panel
docker compose ps
docker compose logs --tail=100 panel ingress
```

出口节点：

```bash
marzban-node status
marzban-node check
```

安装失败时，保留报错并先查看状态；不要删除安装记录。同一安装命令可用于按记录继续，只有本机已保存的配置和凭据才能沿用。
