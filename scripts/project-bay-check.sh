#!/usr/bin/env bash
# ============================================================================
# Project Bay Check — 备份舱体检（舱状态）
# ============================================================================
# 用途：报告舱库最新状态 —— 最新舱时间 / stage / tag / 距上次多久 /
#       归档 sha256 重算比对 / 可否解包。超过 stale_hours 提示「建议代码备份」，
#       但不自动打舱（触发纪律：人提才做）。
# 用法: project-bay-check.sh
# 退出码：0 正常；2 配置缺失
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f backup-bay.conf ] || { echo "缺少 backup-bay.conf" >&2; exit 2; }
# shellcheck source=backup-bay.conf
. ./backup-bay.conf
STALE_HOURS="${stale_hours:-24}"

LATEST=$(ls -1 backups/*.tar.zst backups/*.tar.gz 2>/dev/null | sort | tail -n 1 || true)
if [ -z "$LATEST" ]; then
  echo "📦 舱库为空（backups/ 下无任何舱）。建议在动手前先「代码备份」。"
  exit 0
fi

MANIFEST="${LATEST%.tar.zst}.manifest.json"
[ "$MANIFEST" = "$LATEST" ] && MANIFEST="${LATEST%.tar.gz}.manifest.json"
[ -f "$MANIFEST" ] || { echo "[WARN] manifest 缺失或损坏: $MANIFEST（归档可能不完整，跳过摘要）" >&2; exit 1; }

echo "📦 备份舱状态"
echo "──────────────────────────────────────────────"
echo "最新舱:      $(basename "$LATEST")"
echo "大小:        $(du -h "$LATEST" | cut -f1)"

read -r STAGE TAG_ CREATED SHA < <(python3 - "$MANIFEST" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
b = m["bay"]
print(b.get("stage", "?"), b.get("tag") or "—", b.get("created_utc", "?"), m["integrity"]["archive_sha256"])
PY
)
echo "stage:       $STAGE"
echo "tag:         $TAG_"
echo "created_utc: $CREATED"

NOW_EPOCH=$(date -u +%s)
CREATED_EPOCH=$(date -u -d "$CREATED" +%s 2>/dev/null || echo 0)
if [ "$CREATED_EPOCH" != "0" ]; then
  AGE_HOURS=$(( (NOW_EPOCH - CREATED_EPOCH) / 3600 ))
  echo "距上次:      ${AGE_HOURS} 小时前"
else
  AGE_HOURS=0
fi

# sha256 重算比对
ACTUAL=$(python3 - "$LATEST" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for c in iter(lambda: f.read(65536), b""):
        h.update(c)
print(h.hexdigest())
PY
)
if [ "$ACTUAL" = "$SHA" ]; then
  echo "sha256:      ✓ 一致 (${SHA:0:12}…)"
else
  echo "sha256:      ✗ 不符（归档可能已损坏！）"
fi

# 可解压性
if tar --zstd -tf "$LATEST" >/dev/null 2>&1; then
  echo "可解压:      ✓ ($(tar --zstd -tf "$LATEST" | wc -l) 个条目)"
else
  echo "可解压:      ✗ 失败"
fi

echo "──────────────────────────────────────────────"
if [ "${AGE_HOURS:-0}" -ge "$STALE_HOURS" ]; then
  echo "⚠️  距上次打舱已超过 ${STALE_HOURS} 小时，建议在动手前「代码备份」。"
fi
