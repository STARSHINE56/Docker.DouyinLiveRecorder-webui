# 源码挂载与快速更新（测试版）

默认 `docker-compose.yaml` 保持不变。首次切换先备份服务器目录（尤其 `config/`、`logs/`、`downloads/`、`backup_config/`），确认这四个目录存在，并在无人录制时执行：

```bash
docker compose -f docker-compose.yaml -f docker-compose.fast-update.yaml up -d --build --force-recreate app
```

此操作会重新创建容器，正在录制会中断。覆盖 `/app` 的只读源码挂载不会覆盖镜像中 `/usr/local` 的 Python 依赖及 `/usr/bin` 系统程序；四个数据目录继续独立读写挂载。镜像仍需先按当前系统环境构建。确认容器正常运行后，同一 Git 分支的后续 UI 或无依赖 Python 代码更新：

```bash
./scripts/fast-update.sh
```

脚本拒绝未提交修改、非快进、录制中（除非显式 `--confirm-recording`）及依赖、Compose、Dockerfile、配置格式变更，记录更新前 SHA 至 `.deploy/rollback.tsv`。更新后重建容器但**不构建镜像**；容器重启会中断当前录制。即便检测状态文件未显示录制，也应先确认 FFmpeg 进程的实际情况。

回退上一次更新的代码：

```bash
./scripts/rollback.sh
```

它只回退 Git 代码，保留配置、日志、录像和备份；不兼容配置变更必须单独处理。如果镜像依赖或系统环境变化，先人工检查迁移，再用原 Compose 流程重新构建。脚本没有自动回退配置数据的能力。当前 WebUI 未提供登录验证；公网上线前需通过 HTTPS 反向代理添加身份验证和访问限制，不要直接暴露 5000 端口。
