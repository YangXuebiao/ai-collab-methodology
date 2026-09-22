#!/usr/bin/env bash
# ============================================================================
# Project Bay Backup — 打一个备份舱（git 之外的本地时刻态灾难冗余）
# ============================================================================
# 用途：把「这一刻项目不可再生的全部有意义状态」压成自足单体
#       （归档 .tar.zst + 同名 .manifest.json），单拎出来可离线复现那一刻。
#       纯磁盘文件树拷贝，全程不调用任何版本控制命令（零耦合）。
# 用法: project-bay-backup.sh <阶段|代码|出差> [tag] [note]
# 排除 = 默认可再生产物 + conf exclude_extra；只背不可再生核心。
# 契约：backup-bay@0.1 标准（本仓库人读手册见 docs/backup-bay.md）
# 注：本文件复制自该标准的脚本模板，**仅本行按导出规范改成中性措辞**
#     （模板原文引用的是私有上游的路径，不进公开仓）。其余逐字与模板一致。
# 退出码：0 成功；2 用法/配置错误
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
REALROOT=$(pwd -P)
[ -f backup-bay.conf ] || { echo "缺少 backup-bay.conf（复制自 standards/backup-bay 模板）；abort" >&2; exit 2; }
# shellcheck source=backup-bay.conf
. ./backup-bay.conf

STAGE_CN="${1:?用法: project-bay-backup.sh <阶段|代码|出差> [tag] [note]}"
TAG="${2:-}"
NOTE="${3:-}"
case "$STAGE_CN" in
  阶段) TOKEN=stage ;;
  代码) TOKEN=code ;;
  出差) TOKEN=travel ;;
  *) echo "stage 须 ∈ 阶段/代码/出差" >&2; exit 2 ;;
esac

# tag 只能含 ASCII 安全字符（进舱 id，跨平台/介质安全），且 ≤64
if [ -n "$TAG" ]; then
  if [[ "$TAG" =~ [^A-Za-z0-9._-] ]]; then echo "tag 只能含 A-Za-z0-9._-" >&2; exit 2; fi
  if [ "${#TAG}" -gt 64 ]; then echo "tag 长度须 ≤64" >&2; exit 2; fi
fi
[ "${#NOTE}" -le 512 ] || { echo "note 长度须 ≤512" >&2; exit 2; }

TS=$(date -u +%Y%m%d-%H%M%S)
UTC_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
MACHINE=$(hostname)
ID="${project}-${TOKEN}-${TS}${TAG:+-$TAG}"
[ -e "backups/$ID.tar.zst" ] && { echo "同秒重名已存在，abort（防覆盖回滚点）" >&2; exit 2; }
mkdir -p backups

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ── ① DB dump（尽力而为：容器不可达则告警跳过，不阻断打舱）──────────
DUMP_OK=0
if [ -n "${db_dump_hook:-}" ]; then
  if bash -c "$db_dump_hook" > "$TMP/dump.sql" 2> "$TMP/dump.err"; then
    DUMP_OK=1
    echo "[INFO] DB dump 成功 → dump.sql"
  else
    echo "[WARN] DB dump 失败（容器不可达？）→ 跳过，舱仍成立" >&2
  fi
fi

# ── ② RESTORE.txt（必须入归档）────────────────────────────────────────
printf '恢复：解包本归档到项目根 → %s\n（完整步骤见 docs/backup-bay.md）\n' \
  "${dep_rebuild:-（见 docs/backup-bay.md）}" > "$TMP/RESTORE.txt"

# ── ③ 打项目文件树（排除 backups/.git/可再生 + conf exclude_extra）───
#     先打未压缩 tar（append 只支持未压缩），RESTORE.txt/dump 追加后再 zstd。
EXCLUDES=(--exclude='backups' --exclude='.git' --exclude='node_modules' --exclude='build' \
          --exclude='dist' --exclude='.venv' --exclude='coverage' --exclude='.dart_tool' \
          --exclude='__pycache__' --exclude='*.pyc')
set -f
for x in ${exclude_extra:-}; do EXCLUDES+=(--exclude="$x"); done
set +f
tar -cf "backups/$ID.tar" "${EXCLUDES[@]}" -C "$REALROOT" .

# ── ④ 追加 RESTORE.txt + dump.sql 到归档根 ────────────────────────────
tar -rf "backups/$ID.tar" -C "$TMP" RESTORE.txt
if [ "$DUMP_OK" = 1 ]; then
  tar -rf "backups/$ID.tar" -C "$TMP" dump.sql
fi

# ── ⑤ 压缩为 .tar.zst + 删未压缩中间体 ────────────────────────────────
zstd -q -f "backups/$ID.tar" -o "backups/$ID.tar.zst"
rm -f "backups/$ID.tar"

# ── ⑥ 归档完整性（重算 sha256，非自报）────────────────────────────────
SHA=$(sha256sum "backups/$ID.tar.zst" | awk '{print $1}')
BYTES=$(stat -c%s "backups/$ID.tar.zst")

# ── ⑦ manifest（字段严格对齐 schema，零 git 字段）─────────────────────
export BAY_PROJECT="$project" BAY_ID="$ID" BAY_STAGE="$STAGE_CN" BAY_TAG="$TAG" \
       BAY_CREATED="$UTC_ISO" BAY_MACHINE="$MACHINE" BAY_NOTE="$NOTE" \
       BAY_SECRETS="${secrets_included:-false}" BAY_SHA="$SHA" BAY_BYTES="$BYTES" \
       BAY_EXTRA="$([ "$DUMP_OK" = 1 ] && printf dump.sql)" \
       BAY_EXCLUDED="node_modules/ build/ dist/ .venv/ coverage/ .dart_tool/ __pycache__/ *.pyc ${exclude_extra:-}" \
       BAY_RESTORE="解包到项目根 → ${dep_rebuild:-重建依赖 → 运行}"

python3 - "backups/$ID.manifest.json" <<'PY'
import json, os, sys
def split_env(k):
    return list(dict.fromkeys(x for x in os.environ.get(k, "").split() if x))
m = {
    "format": "backup-bay@0.1",
    "project": os.environ["BAY_PROJECT"],
    "bay": {
        "id": os.environ["BAY_ID"],
        "stage": os.environ["BAY_STAGE"],
        "created_utc": os.environ["BAY_CREATED"],
        "machine": os.environ["BAY_MACHINE"],
    },
    "content": {
        "trees": ["."],
        "extra": split_env("BAY_EXTRA"),
        "excluded_regen": split_env("BAY_EXCLUDED"),
        "secrets_included": os.environ["BAY_SECRETS"].strip().lower() in ("true", "1", "yes"),
    },
    "integrity": {
        "archive_sha256": os.environ["BAY_SHA"],
        "bytes": int(os.environ["BAY_BYTES"]),
        "compression": "zstd",
    },
    "restore": os.environ["BAY_RESTORE"],
}
t = os.environ.get("BAY_TAG", "").strip()
n = os.environ.get("BAY_NOTE", "").strip()
if t:
    m["bay"]["tag"] = t
if n:
    m["bay"]["note"] = n
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(m, f, ensure_ascii=False, indent=2)
print("manifest →", sys.argv[1])
PY

# ── ⑧ 更新 INDEX.md 时间线 ────────────────────────────────────────────
if [ ! -f backups/INDEX.md ]; then
  printf '# 备份舱时间线\n\n| 时间(UTC) | stage | tag | 说明 | archive_sha256 |\n|---|---|---|---|---|\n' > backups/INDEX.md
fi
NOTE_SAFE="${NOTE//|/\\|}"
NOTE_SAFE="${NOTE_SAFE//$'\n'/ }"
printf '| %s | %s | %s | %s | %s |\n' "$(date -u +%F\ %T)" "$STAGE_CN" "$TAG" "$NOTE_SAFE" "$SHA" >> backups/INDEX.md

# ── ⑨ 轮换：代码舱按 retain_code_bays prune（阶段/出差永不自动删）────
retain="${retain_code_bays:-8}"
mapfile -t CODE_BAYS < <(ls -1 backups/"${project}"-code-*.tar.zst 2>/dev/null | sort)
if [ "${#CODE_BAYS[@]}" -gt "$retain" ]; then
  overflow=$(( ${#CODE_BAYS[@]} - retain ))
  for old in "${CODE_BAYS[@]:0:$overflow}"; do
    echo "[INFO] 轮换删除旧代码舱: $(basename "$old")"
    rm -f "$old" "${old%.tar.zst}.manifest.json"
  done
fi

echo "舱已生成: backups/$ID.tar.zst"
echo "manifest: backups/$ID.manifest.json"
