#!/usr/bin/env bash
# ============================================================================
# Project Bay Restore — 校验 / 演练 / 恢复一个备份舱
# ============================================================================
# 用途：把某个舱（归档 + manifest）恢复到那一刻的项目状态。
#       恢复前校验 manifest 存在 + 重算 sha256 比对（非读自述值）。
# 用法:
#   project-bay-restore.sh --dry-run <archive.tar.zst>   演练（解到 temp 不写项目）
#   project-bay-restore.sh <archive.tar.zst>             真恢复（覆盖项目目录，回到备份那一刻）
#   project-bay-restore.sh --yes <archive.tar.zst>       真恢复（跳过确认，慎用）
# 退出码：0 成功；1 校验/一致性失败；2 用法错误
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
REALROOT=$(pwd -P)

DRY=0
YES=0
ARCH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes) YES=1 ;;
    -*) echo "未知参数: $1" >&2; exit 2 ;;
    *) ARCH="$1" ;;
  esac
  shift
done
[ -n "$ARCH" ] || { echo "用法: $0 [--dry-run|--yes] <archive.tar.zst>" >&2; exit 2; }
[ -f "$ARCH" ] || { echo "归档不存在: $ARCH" >&2; exit 2; }

# manifest = 同基名 + .manifest.json（舱 = 归档 + 同名 manifest，必须成对）
MANIFEST="${ARCH%.tar.zst}.manifest.json"
[ "$MANIFEST" = "$ARCH" ] && MANIFEST="${ARCH%.tar.gz}.manifest.json"
[ -f "$MANIFEST" ] || { echo "manifest 不存在（应与归档同名同处）: $MANIFEST" >&2; exit 2; }

# ── ① 校验：重算归档 sha256 与 manifest 比对 ─────────────────────────
echo "[1] 校验归档完整性…"
EXPECTED=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["integrity"]["archive_sha256"])' "$MANIFEST")
ACTUAL=$(python3 - "$ARCH" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for c in iter(lambda: f.read(65536), b""):
        h.update(c)
print(h.hexdigest())
PY
)
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "[ERROR] sha256 不符: 实得 ${ACTUAL:0:16}… ≠ manifest ${EXPECTED:0:16}…" >&2
  exit 1
fi
echo "  ✓ sha256 一致 (${ACTUAL:0:16}…)"

# ── ② 演练 or 真恢复 ──────────────────────────────────────────────────
if [ "$DRY" = 1 ]; then
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  echo "[2] dry-run 演练（解到临时目录，不写项目）…"
  tar --zstd -xf "$ARCH" -C "$TMP"
  echo "  归档内 RESTORE.txt："
  cat "$TMP/RESTORE.txt" 2>/dev/null || echo "  （未找到）"
  echo "  顶层条目抽样："
  # 2026-09-11 P1 同族排查：`| head -20` 在归档顶层条目 >20 时会给 ls 发 SIGPIPE，
  # pipefail 让整条管道返回 141、set -e 直接中止——dry-run 演练半截退出（假红）。
  # awk 读到 EOF，无提前退出。
  ls "$TMP" | awk 'NR<=20 { print }'
  echo "  演练完成（未写入项目目录）。"
else
  if [ "$YES" = 0 ]; then
    read -r -p "确认把项目目录覆盖为舱「$(basename "$ARCH")」的状态？输入 yes 继续: " ANS
    [ "$ANS" = "yes" ] || { echo "已取消"; exit 1; }
  fi
  echo "[2] 解包覆盖到项目根…"
  tar --zstd -xf "$ARCH" -C "$REALROOT"
  echo "恢复完成。依赖重建见归档内 RESTORE.txt："
  cat "$REALROOT/RESTORE.txt" 2>/dev/null || python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["restore"])' "$MANIFEST"
fi
