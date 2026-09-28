# 源码挂载与快速更新（测试版）

默认 `docker-compose.yaml` 保持不变。首次切换先备份服务器目录（尤其 `config/`、`logs/`、`downloads/`、`backup_config/`），确认这四个目录存在，并在无人录制时执行：

```bash
docker compose -f docker-compose.yaml -f docker-compose.fast-update.yaml up -d --build --force-recreate app
```

此操作会重新创建容器，正在录制会中断。覆盖 `/app` 的只读源码挂载不会覆盖镜像中 `/usr/local` 的 Python 依赖及 `/usr/bin` 系统程序；四个数据目录继续独立读写挂载。镜像仍需先按当前系统环境构建。确认容器正常运行后，同一 Git 分支的后续 UI 或无依赖 Python 代码更新：

```bash
bash scripts/fast-update.sh
```

脚本拒绝未提交修改、非快进、录制中或状态文件缺失（除非显式 `--confirm-recording`）及依赖、Compose、Dockerfile、更新脚本或配置格式变更，记录更新前 SHA 至 `.deploy/rollback.tsv`。更新后重建容器但**不构建镜像**；容器重启会中断当前录制。如果新版本无法启动或 HTTP 状态检查失败，脚本会尝试自动恢复旧代码并重新创建旧容器；若旧容器也无法启动，需查看 Docker 日志。即便检测状态文件未显示录制，也应先确认 FFmpeg 进程的实际情况。

回退上一次更新的代码：

```bash
bash scripts/rollback.sh
```

它只回退 Git 代码，保留配置、日志、录像和备份；不兼容配置变更必须单独处理。如果镜像依赖或系统环境变化，先人工检查迁移，再用原 Compose 流程重新构建。脚本没有自动回退配置数据的能力。当前 WebUI 未提供登录验证；公网上线前需通过 HTTPS 反向代理添加身份验证和访问限制，不要直接暴露 5000 端口。

## 公网访问的最小方案

当前 `/api/`、配置、日志和删除接口没有内置登录/CSRF 保护。先在部署用的 Compose 配置中将 `app` 的原 `5000:5000` **替换**为 `127.0.0.1:5000:5000`，不可只额外添加一条端口映射；用 `docker compose -f docker-compose.yaml -f docker-compose.fast-update.yaml config` 核对最终端口。再在宿主机运行 Caddy，给域名配置 HTTPS 和全站认证：

```caddyfile
live.example.com {
    basic_auth {
        star YOUR_CADDY_HASHED_PASSWORD
    }
    reverse_proxy 127.0.0.1:5000
}
```

使用 `caddy hash-password` 生成密码哈希并替换示例文本；不要将明文密码或真实哈希提交到公开仓库。域名需指向服务器并允许 HTTPS 证书验证所需的公网 80/443 连接；若 NAT 只提供其他端口，需要另行配置可用的证书验证方式。确认原先直达 5000 的 NAT 转发和防火墙规则已关闭，且匿名访问返回认证挑战后，再考虑公开地址。不要在本仓库中保存真实 Cookie、WebDAV 凭据或令牌；若已提交到公开仓库，应在对应平台撤销或轮换，删除 Git 历史中的值不能使泄露的凭据重新安全。
