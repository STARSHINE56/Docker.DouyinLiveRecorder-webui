#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.yaml -f docker-compose.fast-update.yaml)
die(){ printf '更新已停止：%s\n' "$*" >&2; exit 1; }
[[ -f docker-compose.fast-update.yaml ]] || die '请先部署源码挂载配置'
branch=$(git symbolic-ref --quiet --short HEAD) || die '当前处于分离 HEAD；请先切换目标分支'
[[ -z $(git status --porcelain --untracked-files=normal) ]] || die '存在未提交文件，请先处理本地修改（不自动覆盖）'
[[ -d config && -d logs && -d downloads && -d backup_config ]] || die '持久化目录不完整，请检查部署目录'
"${compose[@]}" config -q || die 'Compose 配置不正确'
# A stale runtime state is still treated as active: fail closed.
if [[ -f config/runtime_state.json ]] && python3 -c 'import json,sys; s=json.load(open("config/runtime_state.json")); sys.exit(1 if any(m.get("recording_status") in ("starting","recording","recovering","stopping") for m in s.get("monitors",{}).values()) else 0)' ; then :
elif [[ -f config/runtime_state.json && ${1:-} != --confirm-recording ]]; then
  die '检测到正在录制或状态不可读取；重启会中断录制。如已确认，传入 --confirm-recording'
fi
old=$(git rev-parse HEAD)
git fetch origin "$branch" || die '拉取远端失败，工作区未更新'
new=$(git rev-parse FETCH_HEAD)
git merge-base --is-ancestor "$old" "$new" || die '远端不是当前版本的快进更新，请人工检查'
[[ "$old" != "$new" ]] || { echo '已经是最新版本'; exit 0; }
changes=$(git diff --name-only "$old" "$new")
if printf '%s\n' "$changes" | grep -Eq '^(requirements[^/]*|Dockerfile|docker-compose.*|config/|backup_config/|migrations/|pyproject.toml|setup.py|package.json|package-lock.json|\.dockerignore)'; then
  die '更新涉及依赖、镜像、部署或配置格式；请检查迁移并重新构建'
fi
# Syntax check changed Python sources without modifying the worktree.
while IFS= read -r file; do
  if [[ $file == *.py ]] && git cat-file -e "$new:$file" 2>/dev/null; then
    git show "$new:$file" | python3 -c 'import ast,sys; ast.parse(sys.stdin.read())' || die "Python 语法检查失败：$file"
  fi
done <<< "$changes"
mkdir -p .deploy
printf '%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$branch" "$old" >> .deploy/rollback.tsv
# Persist rollback marker outside Git; user data is untouched.
git merge --ff-only "$new" || die '合并失败，未重启容器'
if ! "${compose[@]}" up -d --no-deps --no-build --force-recreate app; then
  die '容器重启失败；请运行 scripts/rollback.sh 恢复上一版代码'
fi
"${compose[@]}" ps --status running app | grep -q douyin-live-recorder-webui || die '服务未运行；请查看 docker compose logs，并执行回退脚本'
# Dockerfile has no HEALTHCHECK; exercise HTTP endpoint explicitly.
for attempt in 1 2 3 4 5; do
  if "${compose[@]}" exec -T app python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:5000/api/status",timeout=3)' >/dev/null 2>&1; then
    echo "已更新至 $new；旧版本 $old；录制重启可能导致中断"; exit 0
  fi
  sleep 2
done
die 'HTTP 状态检查失败；请查看日志并执行 scripts/rollback.sh'
