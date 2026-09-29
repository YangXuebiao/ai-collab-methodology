#!/usr/bin/env bash
# ============================================================================
# Project Bay Backup — 打一个备份舱（git 之外的本地时刻态灾难冗余）
# ============================================================================
# 用途：把「这一刻项目不可再生的全部有意义状态」压成自足单体
#       （归档 .tar.zst + 同名 .manifest.json），单拎出来可离线复现那一刻。
#       纯磁盘文件树拷贝，全程不调用任何版本控制命令（零耦合）。
# 用法: project-bay-backup.sh <阶段|代码|出差> [tag] [note]
# 排除 = 默认可再生产物 + conf exclude_extra；只背不可再生核心。
# 契约：_framework/standards/backup-bay/spec/（README.md §0–§4 + lifecycle.md §5–§9）· backup-bay 0.2-draft
# 退出码：0 成功；2 用法/配置错误
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
REALROOT=$(pwd -P)
[ -f backup-bay.conf ] || { echo "缺少 backup-bay.conf（复制自 standards/backup-bay 模板）；abort" >&2; exit 2; }
# shellcheck source=backup-bay.conf
. ./backup-bay.conf

# ── 轮换保留区间（契约 spec §7）：默认 [3,7] ──────────────────────────────
# 下界 3 = 保底（丢掉一个还有两个）；上界 7 = 防舱库膨胀（实测先例：同一份快照打三次 ≈418M）。
RETAIN_MIN="${retain_code_min:-3}"
RETAIN_MAX="${retain_code_max:-7}"
# ⚠️ **必须是"是纯数字"才放行**（`2026-09-29 独立审计 R1 #6`）：第一版只做 `-lt` 比较，
#    而 `[ "abc" -lt 1 ]` 在 bash 里**不返回非零**（只往 stderr 吐一行"需要整数表达式"）
#    ⇒ `retain_code_max=abc` 时**轮换被静默关掉**、连打 5 个舱 `_Trash` 里 0 个、**三道门禁全绿**。
#    **fail-open 是这一族里最贵的形状** —— 配置写错必须当场停，不许猜。
for _v in "$RETAIN_MIN" "$RETAIN_MAX" "${size_warn_ratio:-2}"; do
  case "$_v" in
    ''|*[!0-9]*) echo "配置非法：retain_code_min=$RETAIN_MIN retain_code_max=$RETAIN_MAX size_warn_ratio=${size_warn_ratio:-2}（须为纯数字）" >&2; exit 2 ;;
  esac
done
if [ "$RETAIN_MIN" -lt 1 ] || [ "$RETAIN_MAX" -lt "$RETAIN_MIN" ]; then
  echo "轮换区间非法：retain_code_min=$RETAIN_MIN retain_code_max=$RETAIN_MAX（须 1 ≤ min ≤ max）" >&2
  exit 2
fi
# 旧键**只告警、不静默忽略**（契约 0.2 把保留数从单值 8 改成区间 3~7）——
# 静默忽略是"零痕迹"那一族缺陷，正是本仓反复吃亏的形状。
if [ -n "${retain_code_bays:-}" ]; then
  echo "[WARN] conf 的 retain_code_bays 已废弃（改用 retain_code_min/retain_code_max）；本次按 [${RETAIN_MIN},${RETAIN_MAX}] 执行" >&2
fi
SIZE_WARN_RATIO="${size_warn_ratio:-2}"

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

# ── ⑨ 体积判据（契约 spec §7bis）：新舱 > 上一舱 × ratio ⇒ 告警 ─────────
# 依据：体积突增是「不可再生分级判据失准」的**可观测信号**。
# 实测先例 fppcDockerUpdate：10M → 142M（14×），成因是一条**可再生的** stale tar（125.9M）混进来。
# ⚠️ 「上一舱」按 **mtime** 取，不按文件名 `sort` 取 —— 同一秒内打两个不同 tag 的舱时，
#    文件名序**不等于**时间序（`-big` / `-same` / `-small` 按名字排，`small` 反而排最后）。
#    这个缺陷是 `bay-policy` 的**反例对照**自己抓出来的（拿同量级舱必须静默，结果它误报）。
PREV=$(ls -1t backups/"${project}"-*.tar.zst 2>/dev/null | command grep -v "/${ID}\.tar\.zst$" | head -n 1 || true)
if [ -n "$PREV" ] && [ -f "$PREV" ]; then
  PREV_BYTES=$(stat -c%s "$PREV")
  if [ "$PREV_BYTES" -gt 0 ] && [ "$BYTES" -gt $(( PREV_BYTES * SIZE_WARN_RATIO )) ]; then
    echo "[WARN] 体积判据：本次 ${BYTES} 字节 > 上一舱 ${PREV_BYTES} 字节 × ${SIZE_WARN_RATIO}（上一舱: $(basename "$PREV")）" >&2
    echo "[WARN]   ⇒ 「不可再生」分级判据可能失准。请人工看一次：" >&2
    echo "[WARN]     tar -tvf <本舱> | sort -k3 -n | tail -20   # 确认没有大型可再生件混入" >&2
    echo "[WARN]     确认是可再生件 ⇒ 加进 backup-bay.conf 的 exclude_extra（不是删舱）" >&2
  fi
fi

# ── ⑩ 轮换：代码舱 prune 到 retain_code_max（阶段/出差永不自动删）──────
# ⚠️ 轮转**不物理删**（`Work/CLAUDE.md §二` 删除两步）：`mv` 进 `backups/_Trash/<UTC时间戳>-rotate/`。
#    放 `backups/` 内是**刻意的**：契约要求「自完备 / 位置不敏感」，不能依赖任何上级目录的 `_Trash`；
#    而 `backups/` 整体被 tar 排除，旧舱不会被卷进新舱。
# ⚠️ **按 mtime（`ls -1t`，最新在前）**，不按文件名 `sort` —— 与 ⑨ 的体积判据**同一族缺陷**
#    （`2026-09-29 独立审计 R1 #5` 实测）：同一秒内打两个不同 tag 的舱时，**文件名序 ≠ 时间序**，
#    于是这一版会**trash 掉更新、保留更旧**的那个。`bay-policy` 只断言**数量**不断言**保留谁**，
#    所以这个缺陷**当时是绿的**。本版把"保留谁"也交给 mtime，并加断言。
mapfile -t CODE_BAYS < <(ls -1t backups/"${project}"-code-*.tar.zst 2>/dev/null)
if [ "${#CODE_BAYS[@]}" -gt "$RETAIN_MAX" ]; then
  overflow=$(( ${#CODE_BAYS[@]} - RETAIN_MAX ))
  ROT_TS=$(date -u +%Y%m%d-%H%M%S)
  TRASH_DIR="backups/_Trash/${ROT_TS}-rotate"
  mkdir -p "$TRASH_DIR"
  for old in "${CODE_BAYS[@]:RETAIN_MAX}"; do
    echo "[INFO] 轮换：旧代码舱移入 $TRASH_DIR（不物理删）: $(basename "$old")"
    mv -f "$old" "$TRASH_DIR/"
    [ -f "${old%.tar.zst}.manifest.json" ] && mv -f "${old%.tar.zst}.manifest.json" "$TRASH_DIR/"
  done
  echo "[INFO] 轮换 ${overflow} 个 → $TRASH_DIR（90 天后本人清空；见 Work/CLAUDE.md §二 删除两步）"
fi
KEPT=$(ls -1 backups/"${project}"-code-*.tar.zst 2>/dev/null | wc -l)
[ "$KEPT" -lt "$RETAIN_MIN" ] && echo "[WARN] 代码舱只剩 ${KEPT} 个 < 下界 ${RETAIN_MIN}（保底被击穿，查 retain_code_max 设置）" >&2

echo "舱已生成: backups/$ID.tar.zst"
echo "manifest: backups/$ID.manifest.json"
