#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
die(){ printf '回退已停止：%s\n' "$*" >&2; exit 1; }
[[ -z $(git status --porcelain --untracked-files=normal) ]] || die '有未提交修改，请先处理；不会强制覆盖'
[[ -f .deploy/rollback.tsv ]] || die '没有可用的更新前版本记录'
read -r _ branch old < <(tail -n 1 .deploy/rollback.tsv)
[[ $(git branch --show-current) == "$branch" ]] || die "当前分支与记录的 $branch 不一致"
git cat-file -e "$old^{commit}" || die '旧版本对象不存在'
current=$(git rev-parse HEAD)
git merge-base --is-ancestor "$old" "$current" || die '当前版本和回退记录不在同一历史上'
if [[ -f config/runtime_state.json ]] && ! python3 -c 'import json,sys; s=json.load(open("config/runtime_state.json")); sys.exit(1 if any(m.get("recording_status") in ("starting","recording","recovering","stopping") for m in s.get("monitors",{}).values()) else 0)'; then
  [[ ${1:-} == --confirm-recording ]] || die '正在录制或状态不可读取；重启会中断录制，确认后传 --confirm-recording'
fi
# Rollback only code. Config, logs, downloads and backup_config stay as-is.
git reset --keep "$old" || die '代码无法安全回退'
compose=(docker compose -f docker-compose.yaml -f docker-compose.fast-update.yaml)
"${compose[@]}" up -d --no-deps --no-build --force-recreate app || die '代码已回退，但服务重启失败，请检查日志'
"${compose[@]}" ps --status running app | grep -q douyin-live-recorder-webui || die '代码已回退，但服务未运行'
echo "代码已回退至 $old；此前版本 $current。配置和录像未回退。"
