# 面板迁移

迁移入口独立于全新安装。迁移支持本仓库所用定制 v0.8.4 的 SQLite 数据，包含管理员、用户、到期时间、已用流量、重置记录、UUID、入站授权、Host、节点、JWT、节点 TLS 身份和应用数据目录。源与目标关键应用文件必须匹配。MySQL、不同数据库迁移版本和数据超过 256 MiB 时会拒绝导入。

## 1. 预演备份（旧主控继续运行）

先在新服务器按 README 部署空面板。新管理域名和订阅域名指向新服务器，不改旧域名，也不配置转发。

在旧主控服务器执行，只把 `旧面板容器名` 和 `旧主控公网IP` 替换成实际值。`docker ps` 可查看容器名称。

```bash
sudo -i
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.0.0/migrate.sh) backup --container 旧面板容器名 --source-address 旧主控公网IP --source-address 旧主控域名 --output /root/panel-preview.mzb
```

按隐藏提示设置至少 12 位加密口令并妥善另存。工具通过只读挂载导出，SQLite 使用在线一致性快照；它不会修改源数据库、源配置或启动第二个主控。迁移加密依赖缺失时会安装 `python3-cryptography`。密码、数据库和私钥不打印到终端。

通过自己的 SSH/SCP 把 `.mzb` 复制到新主控，不放到公开下载地址。在新主控执行：

```bash
marzban-migrate restore --bundle /root/panel-preview.mzb
marzban-migrate verify
```

恢复会替换空面板的新管理员，之后使用原面板管理员登录。只有没有业务用户和节点的空目标或已有同身份隔离验收实例可以被恢复覆盖。目标原数据保留在 `releases/`。

验收模式的节点在数据库中临时禁用，核心和调度任务不会启动，订阅返回 503，业务修改返回 409。还会用 Docker 内部网络阻止后台连接外部节点。可以查看用户、到期时间、流量和授权；工具对所有表执行摘要校验，仅节点的运行状态被明确排除。预演期间旧主控继续运行，后来新增的流量需以最终快照为准。

## 2. 切换前检查

- 如果旧主控也作为用户出口，先处理相关线路。工具遇到仍有用户授权的本机出口会阻止 `freeze` 和 `activate`。核对后确实接受中断才加 `--ack-local-exits-reviewed`；它不会自动改写出口地址。
- 检查订阅中的 Host 地址和所有证书文件。工具原样保留 Host，不会把旧出口误改成新主控地址。核心引用的证书会随加密包收集并改为目标数据目录路径；自定义模板必须位于应用数据目录内。
- 各出口的云安全组需要给新主控 IP 放行管理与 Xray API 端口。旧主控在最终接管前必须停止，不能让两个面板同时管理同一个节点实例。

## 3. 最终停机快照与接管

在旧主控执行 `freeze`，它会在确认后执行 `docker stop` 并导出最终快照，不自动重启源容器：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-panel/v1.0.0/migrate.sh) freeze --container 旧面板容器名 --source-address 旧主控公网IP --source-address 旧主控域名 --output /root/panel-final.mzb
```

把最终包复制到新主控后：

```bash
marzban-migrate restore --bundle /root/panel-final.mzb
marzban-migrate verify
marzban-migrate activate
```

`activate` 会要求输入源容器标识的末 12 位，并确认旧主控仍停止。停机快照记录只证明导出时源容器已停止；脚本不持有旧服务器 SSH 凭据，无法代替操作者检查此刻的旧服务器。没有最终停机记录的预演包禁止接管。

## 4. 出口节点改管理来源

使用公开节点安装器 v1.1.0，在每个由该安装器管理的出口节点执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Taozi-succes/marzban-node-installer/v1.1.0/bootstrap.sh) switch-panel
```

输入新面板 HTTPS 地址、新主控公网 IP、新面板中的原管理员账号与密码。它核对管理 TLS 身份、节点编号、地址、端口和入站，再更新本地管理来源规则和安装记录；不会重建代理密钥、修改用户授权或发布全量核心配置。不接受直接切换到独立空面板。

旧式手动部署的节点没有本安装器状态文件，需要按原部署方式更新管理来源防火墙；节点管理 TLS 证书因数据库迁移而保持一致。公网客户端代理地址是否改变，取决于出口自身有没有改变，与主控域名无关。

## 5. 验收与回退

新面板接管后检查用户数、到期时间、流量、入站授权；逐台验证节点连接、订阅、实际出口 IP 和新增流量计量。确认后通知用户使用新订阅域名，不需要旧域名转发。

如果 `restore` 失败，工具尝试恢复目标原状态；若系统突然断电等导致事务未完成，运行 `marzban-migrate recover`。被替换的数据保留在目标 `releases/`。`activate` 中断可以重新执行继续，不重新导入数据。

一旦新主控已计量或接收业务修改，不能直接启动旧主控回退，否则会丢失新数据且出现双主控。先停止新主控、导出新状态并制定反向恢复流程，再恢复唯一主控。
