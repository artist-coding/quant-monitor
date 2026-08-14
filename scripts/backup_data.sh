#!/usr/bin/env bash
#
# 数据备份：给 data/ 里无法从 git 恢复的东西做快照，传到私有仓库的 Release。
#
# 为什么不走主仓库：主仓库是 public，而这些是 Tushare 行情数据（转发受其条款约束）；
# 而且行情库 ~2GB，远超 GitHub 单文件 100MB 的上限。所以 data/ 永不入库，
# 备份走独立的**私有**仓库，以 Release 附件形式按日期打 tag（单附件上限 2GB）。
#
# 用法：
#   scripts/backup_data.sh                 备份并上传
#   scripts/backup_data.sh --local-only    只在本地出快照，不上传
#   scripts/backup_data.sh --keep 5        本地保留最近 5 份（默认 2）
#
# 环境变量覆盖：
#   BACKUP_REPO   目标私有仓库（默认 artist-coding/quant-monitor-data）
#   BACKUP_DIR    本地备份目录（默认 /root/backups/quant-monitor）
#   DB_PATH       行情库路径（默认 <仓库根>/data/stock_data.db）
#   MANUAL_DIRS   要打包的手工数据目录，空格分隔（默认 amv kimi_research daily_scans）
#
# 挂定时任务（每周日 03:00）：
#   0 3 * * 0 /root/quant-monitor/scripts/backup_data.sh >> /root/backups/quant-monitor/cron.log 2>&1

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_REPO="${BACKUP_REPO:-artist-coding/quant-monitor-data}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups/quant-monitor}"
DB_PATH="${DB_PATH:-$REPO_ROOT/data/stock_data.db}"
MANUAL_DIRS="${MANUAL_DIRS:-amv kimi_research daily_scans}"
KEEP_LOCAL=2
UPLOAD=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --local-only) UPLOAD=0; shift ;;
        --keep)       KEEP_LOCAL="$2"; shift 2 ;;
        -h|--help)    sed -n '2,28p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)            echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    esac
done

DATE="$(date +%Y%m%d)"
TAG="data-$DATE"
SNAPSHOT="$BACKUP_DIR/stock_data_$DATE.db"
DB_ASSET="$SNAPSHOT.zst"
MANUAL_ASSET="$BACKUP_DIR/data_manual_$DATE.tar.zst"
SUMS="$BACKUP_DIR/SHA256SUMS.txt"
STATS="$BACKUP_DIR/.stats.$$"

# GitHub 单个 Release 附件上限 2GiB
MAX_ASSET=$((2 * 1024 * 1024 * 1024))

log()  { echo "[$(date +%H:%M:%S)] $*"; }
die()  { echo "错误：$*" >&2; exit 1; }
trap 'rm -f "$STATS"' EXIT

# ---------- 前置检查 ----------
command -v python3 >/dev/null || die "需要 python3（快照用 SQLite 在线备份 API，机器上没有 sqlite3 命令行）"
command -v zstd    >/dev/null || die "需要 zstd"
[[ -f "$DB_PATH" ]] || die "找不到行情库：$DB_PATH"
if [[ $UPLOAD -eq 1 ]]; then
    command -v gh >/dev/null || die "需要 gh（或加 --local-only 只做本地备份）"
    gh auth status >/dev/null 2>&1 || die "gh 未登录，先跑 gh auth login"
fi

mkdir -p "$BACKUP_DIR"

# 快照要和原库同盘同量级，先确认空间够（压缩前后都要放得下，留 1.2 倍余量）
db_bytes=$(stat -c %s "$DB_PATH")
free_bytes=$(($(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}') * 1024))
(( free_bytes > db_bytes * 12 / 10 )) || \
    die "空间不足：$BACKUP_DIR 剩 $((free_bytes/1024/1024))M，库本身 $((db_bytes/1024/1024))M"

# ---------- 1. 快照 ----------
# 必须走 SQLite 在线备份 API 而不是 cp：uvicorn 常驻连着这个库，
# 直接复制可能拿到写到一半的页，得到一个能打开但内容错乱的文件。
log "快照 $DB_PATH → $SNAPSHOT"
python3 - "$DB_PATH" "$SNAPSHOT" "$STATS" <<'PY'
import sqlite3, sys, os

src, dst, stats = sys.argv[1], sys.argv[2], sys.argv[3]
if os.path.exists(dst):
    os.remove(dst)

s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close()
s.close()

c = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    # 校验不过就别上传了——一个坏的备份比没有备份更危险，因为它会让人以为有救
    print(f"integrity_check 失败：{ok}", file=sys.stderr)
    sys.exit(1)

try:
    rows, lo, hi = c.execute(
        "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM daily_kline"
    ).fetchone()
except sqlite3.Error:
    rows, lo, hi = 0, "?", "?"
c.close()

with open(stats, "w") as f:
    f.write(f"{rows}\n{lo}\n{hi}\n{os.path.getsize(dst)}\n")
print(f"  integrity_check ok，daily_kline {rows:,} 行，{lo}–{hi}")
PY

{ read -r ROWS; read -r DATE_LO; read -r DATE_HI; read -r RAW_BYTES; } < "$STATS"

# ---------- 2. 压缩 ----------
log "压缩行情库（zstd -12，约需 1-3 分钟）"
# -f：同一天重跑是正常场景（对应下面 Release 已存在时的 --clobber 分支），
# 而 zstd 默认拒绝覆盖已有文件，不加 -f 会在第二次运行时直接失败。
zstd -12 -T0 --rm -qf "$SNAPSHOT" -o "$DB_ASSET"

# 手工数据：amv 是逐次手工录入的，丢了无法从任何 API 重新拉回来
present=()
for d in $MANUAL_DIRS; do
    [[ -d "$REPO_ROOT/data/$d" ]] && present+=("$d")
done
if [[ ${#present[@]} -gt 0 ]]; then
    log "打包手工数据：${present[*]}"
    tar -C "$REPO_ROOT/data" -cf - "${present[@]}" | zstd -12 -T0 -qf -o "$MANUAL_ASSET"
else
    echo "警告：$MANUAL_DIRS 一个都不存在，跳过手工数据" >&2
    MANUAL_ASSET=""
fi

assets=("$DB_ASSET")
[[ -n "$MANUAL_ASSET" ]] && assets+=("$MANUAL_ASSET")

for a in "${assets[@]}"; do
    sz=$(stat -c %s "$a")
    (( sz <= MAX_ASSET )) || die "$(basename "$a") 有 $((sz/1024/1024))M，超过 Release 单附件 2GB 上限"
done

( cd "$BACKUP_DIR" && sha256sum "$(basename "$DB_ASSET")" \
    ${MANUAL_ASSET:+"$(basename "$MANUAL_ASSET")"} > "$SUMS" )
assets+=("$SUMS")

log "产物："
ls -lh "${assets[@]}" | sed 's/^/  /'

# ---------- 3. 上传 ----------
if [[ $UPLOAD -eq 0 ]]; then
    log "--local-only：跳过上传"
else
    # 目标必须是私有仓库——公开会把 Tushare 数据转发出去
    if [[ "$(gh api "repos/$BACKUP_REPO" --jq .private 2>/dev/null)" != "true" ]]; then
        die "$BACKUP_REPO 不是私有仓库（或读不到），拒绝上传"
    fi

    db_mb=$(( $(stat -c %s "$DB_ASSET") / 1024 / 1024 ))
    notes="全市场日线库快照 + 手工数据。

- \`$(basename "$DB_ASSET")\` — ${db_mb}M（原 $((RAW_BYTES/1024/1024))M），daily_kline $ROWS 行，覆盖 $DATE_LO–$DATE_HI，\`PRAGMA integrity_check\` 通过
- \`$(basename "${MANUAL_ASSET:-（无）}")\` — 手工数据：${present[*]:-无}
- \`SHA256SUMS.txt\` — 校验和

由 scripts/backup_data.sh 生成（SQLite 在线备份 API）。恢复步骤见 README。"

    if gh release view "$TAG" --repo "$BACKUP_REPO" >/dev/null 2>&1; then
        log "Release $TAG 已存在，覆盖附件"
        gh release upload "$TAG" "${assets[@]}" --repo "$BACKUP_REPO" --clobber
    else
        log "创建 Release $TAG 并上传（$db_mb M，视带宽可能要几分钟）"
        gh release create "$TAG" "${assets[@]}" \
            --repo "$BACKUP_REPO" \
            --title "数据快照 ${DATE:0:4}-${DATE:4:2}-${DATE:6:2}" \
            --notes "$notes"
    fi

    log "已上传：https://github.com/$BACKUP_REPO/releases/tag/$TAG"
fi

# ---------- 4. 清理本地旧备份 ----------
# 云端的 Release 是长期归档，本地只留最近几份用来防误删
for pat in 'stock_data_*.db.zst' 'data_manual_*.tar.zst'; do
    mapfile -t old < <(find "$BACKUP_DIR" -maxdepth 1 -name "$pat" -printf '%f\n' \
                       | sort -r | tail -n "+$((KEEP_LOCAL + 1))")
    for f in "${old[@]:-}"; do
        [[ -n "$f" ]] || continue
        log "清理本地旧备份：$f"
        rm -f "$BACKUP_DIR/$f"
    done
done

log "完成"
