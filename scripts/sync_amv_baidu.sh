#!/usr/bin/env bash
# ============================================================
#  从百度网盘分享链接拉当天的活跃市值表，按日期归档到 data/amv/baidu/
#
#  只负责"把文件搞到本地"，不碰数据库。入库由 scripts/sync_amv.py 接手，
#  它读本脚本 stdout 上的 FILE= 那行。日志一律走 stderr，别往 stdout 写。
#
#  原理：百度网盘没有"直接下载分享文件"的公开接口，只能走官方 App 的路子——
#  先把分享文件转存到自己的网盘，再从自己的网盘下载，最后删掉中转文件。
#  驱动是 BaiduPCS-Go（静态编译的 Go 二进制，无依赖）。
#
#  stdout 契约（最后三行，供 sync_amv.py 解析）：
#      STATUS=archived|unchanged|failed
#      FILE=<归档文件绝对路径>        # failed 时没有这行
#      REASON=<失败原因>              # 只有 failed 时才有
#  退出码：0 = archived 或 unchanged；1 = failed
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 直接手工跑本脚本时没人给环境变量：systemd 走 EnvironmentFile，
# scripts/sync_amv.py 走 modules/__init__ 里的 load_dotenv，只有裸跑 .sh 两头不靠。
# 只挑 AMV_BAIDU_* 那几行读，不整份 source —— .env 里别的值带 # 和空格，
# source 进来会被当注释截断或被拆词。
if [ -f "$ROOT/.env" ]; then
  while IFS= read -r _line; do
    _key="${_line%%=*}"
    # 已经在环境里的不覆盖：显式 env 和 systemd 的 EnvironmentFile 优先。
    [ -n "${!_key:-}" ] && continue
    export "$_key=${_line#*=}"
  done < <(grep -E '^AMV_BAIDU_[A-Za-z_]+=' "$ROOT/.env")
  unset _line _key
fi

# ==================== 配置（全部可用环境变量覆盖）====================
# 分享链接与提取码没有默认值，且**不进仓库**——主仓库是公开的。
# 放 .env 里，systemd 单元用 EnvironmentFile 带进来。
SHARE_URL="${AMV_BAIDU_SHARE_URL:-}"
SHARE_PWD="${AMV_BAIDU_SHARE_PWD:-}"

# 网盘里的中转目录。脚本每次会**清空**它，别放别的东西。
PAN_WORKDIR="${AMV_BAIDU_PAN_WORKDIR:-/auto_dl}"
ARCHIVE_DIR="${AMV_BAIDU_ARCHIVE_DIR:-$ROOT/data/amv/baidu}"
FILE_PREFIX="${AMV_BAIDU_PREFIX:-活跃市值}"
KEEP_DAYS="${AMV_BAIDU_KEEP_DAYS:-90}"       # 本地归档保留天数，0 = 永久
CLEAN_PAN="${AMV_BAIDU_CLEAN_PAN:-1}"        # 下载后删除网盘中转文件
CLEAR_RECYCLE="${AMV_BAIDU_CLEAR_RECYCLE:-1}" # 顺带清空回收站（中转文件会堆在里面）
DOWNLOAD_MODE="${AMV_BAIDU_MODE:-locate}"    # locate / pcs / stream，失败自动回退
NOTIFY_CMD="${AMV_BAIDU_NOTIFY_CMD:-}"       # 失败告警，消息在 $MSG 里；留空只写日志

export BAIDUPCS_GO_CONFIG_DIR="${BAIDUPCS_GO_CONFIG_DIR:-$HOME/.config/BaiduPCS-Go}"
LOG_FILE="${AMV_BAIDU_LOG_FILE:-$ROOT/data/logs/sync_amv_baidu-$(date +%Y%m).log}"
LOCK_FILE="${AMV_BAIDU_LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/quant-monitor-amv.lock}"
# ====================================================================

export LANG="${LANG:-C.UTF-8}"; export LC_ALL="$LANG"

# 本机 socks5 代理会打断数据同步通路，且报错都指向错误方向（表现为连接超时，
# 看着像对端挂了）。百度网盘是境内服务，走代理有害无益，默认清掉。
# 万一哪天确实要走代理，AMV_BAIDU_KEEP_PROXY=1 保留。
if [ "${AMV_BAIDU_KEEP_PROXY:-0}" != "1" ]; then
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
fi

TODAY="$(date +%Y%m%d)"
TMPDL=""

mkdir -p "$ARCHIVE_DIR" "$(dirname "$LOG_FILE")"

# 日志走 stderr：stdout 留给 STATUS=/FILE= 契约，混在一起 sync_amv.py 就解析不了。
log()  { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE" >&2; }
cleanup() { [ -n "$TMPDL" ] && rm -rf "$TMPDL"; }
trap cleanup EXIT

die() {
  log "❌ 失败: $*"
  printf 'STATUS=failed\nREASON=%s\n' "$*"
  if [ -n "$NOTIFY_CMD" ]; then
    MSG="活跃市值下载失败($(date '+%F %T')): $*" "${SHELL:-/bin/bash}" -c "$NOTIFY_CMD" \
      || log "（告警命令本身也失败了）"
  fi
  exit 1
}

# ---- 找 BaiduPCS-Go ----
if [ -n "${PCS_BIN:-}" ]; then
  PCS="$PCS_BIN"
else
  PCS=""
  for cand in /usr/local/bin/BaiduPCS-Go "$HOME/.local/bin/BaiduPCS-Go" "$(command -v BaiduPCS-Go 2>/dev/null)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { PCS="$cand"; break; }
  done
fi

# ---- 防止两次运行撞车 ----
exec 9>"$LOCK_FILE" || die "打不开锁文件 $LOCK_FILE"
flock -n 9 || { log "上一次任务还在跑，本次跳过"; printf 'STATUS=unchanged\n'; exit 0; }

log "===== 开始 ====="

# ---- 0. 前置检查 ----
[ -n "$SHARE_URL" ] || die "没有配 AMV_BAIDU_SHARE_URL（写进 .env，见 .env.example）"
[ -n "$SHARE_PWD" ] || die "没有配 AMV_BAIDU_SHARE_PWD（写进 .env，见 .env.example）"
[ -n "$PCS" ] && [ -x "$PCS" ] \
  || die "找不到 BaiduPCS-Go。装法见 deploy/README.md，或用 PCS_BIN 指定路径"

# who 未登录时**退出码仍是 0**，只有输出里 uid 是 0，必须看文本。
WHO="$("$PCS" who 2>&1)"
echo "$WHO" | grep -q "当前帐号 uid: [1-9]" \
  || die "登录态无效，需要重新写入 Cookie（含 STOKEN）。BaiduPCS-Go 输出：$WHO"
log "账号: $(echo "$WHO" | tr -d '\n')"

# ---- 1. 准备网盘中转目录 ----
"$PCS" mkdir "$PAN_WORKDIR" >>"$LOG_FILE" 2>&1   # 已存在会报错，忽略即可

# cd 会 Config.Save()，工作目录跨进程持久化；transfer 只能转存到当前工作目录。
CD_OUT="$("$PCS" cd "$PAN_WORKDIR" 2>&1)"
echo "$CD_OUT" | grep -q "改变工作目录: $PAN_WORKDIR" \
  || die "无法切换到网盘目录 $PAN_WORKDIR。输出：$CD_OUT"

# ---- 2. 清空中转目录（关键：不清会因同名文件转存失败）----
"$PCS" rm "$PAN_WORKDIR/*" >>"$LOG_FILE" 2>&1 || true

# ---- 3. 转存分享文件 ----
# BaiduPCS-Go 转存失败时**退出码仍是 0**，必须看输出文本。
# 完整输出形如 "分享链接转存到网盘成功, 保存了 xxx 到当前目录" /
# "分享链接转存到网盘失败: <原因>"（v4.0.2 源码 internal/pcscommand/transfer.go）。
log "转存中…"
TR_OUT="$("$PCS" transfer "$SHARE_URL" "$SHARE_PWD" 2>&1)"
echo "$TR_OUT" >>"$LOG_FILE"

if echo "$TR_OUT" | grep -q "转存到网盘失败"; then
  case "$TR_OUT" in
    *同名文件*|*文件重复*)
        die "中转目录没清干净，网盘里已有同名文件。手动清一下：$PCS rm '$PAN_WORKDIR/*'" ;;
    *STOKEN*)
        die "Cookie 里缺 STOKEN。只写 BDUSS 登录能过 who 但转存必失败，要用完整 Cookie 重登" ;;
    *已失效*|*页面不存在*)
        die "分享链接已失效或被取消，需要向上游要新链接。输出：$TR_OUT" ;;
    *提取码*|*非法*)
        die "链接或提取码不对。输出：$TR_OUT" ;;
    *验证*)
        die "触发了百度的人机验证，稍后再试（别加高频率）。输出：$TR_OUT" ;;
    *)  die "转存失败。输出：$TR_OUT" ;;
  esac
fi
echo "$TR_OUT" | grep -q "转存到网盘成功" || die "转存结果无法确认。输出：$TR_OUT"
log "转存成功"

# ---- 4. 下载到本地临时目录 ----
TMPDL="$(mktemp -d)"
log "下载中…"
"$PCS" download "$PAN_WORKDIR" --saveto "$TMPDL" --mode "$DOWNLOAD_MODE" --mtime >>"$LOG_FILE" 2>&1

# 取最大的那个文件，不是第一个：包里可能夹着说明文件，find 的顺序不保证。
pick_src() { find "$TMPDL" -type f -size +0c -printf '%s %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-; }
SRC="$(pick_src)"

# locate 模式偶尔 "user is not authorized"，回退一次
if [ -z "$SRC" ] && [ "$DOWNLOAD_MODE" = "locate" ]; then
  log "locate 模式没拿到文件，回退到 pcs 模式重试…"
  "$PCS" download "$PAN_WORKDIR" --saveto "$TMPDL" --mode pcs --mtime >>"$LOG_FILE" 2>&1
  SRC="$(pick_src)"
fi

[ -n "$SRC" ] || die "下载后本地没有文件，详见日志 $LOG_FILE"
log "已下载: $(basename "$SRC")  ($(du -h "$SRC" | cut -f1))"

# ---- 5. 去重 + 按日期归档 ----
# 扩展名必须从 basename 上取。直接对全路径做 ${SRC##*.} 的话，mktemp -d
# 造出来的目录名本身带点（/tmp/tmp.AbCdEf），无扩展名的文件会得到
# "AbCdEf/auto_dl/活跃市值" 这种"扩展名"，cp 静默失败而日志照报 ✅ 已归档。
SRC_BASE="$(basename "$SRC")"
EXT="${SRC_BASE##*.}"
[ "$EXT" = "$SRC_BASE" ] && EXT="xlsx"     # 真的没有扩展名
DEST="$ARCHIVE_DIR/${FILE_PREFIX}_${TODAY}.${EXT}"
NEW_MD5="$(md5sum "$SRC" | cut -d' ' -f1)"

PREV="$(find "$ARCHIVE_DIR" -maxdepth 1 -type f -name "${FILE_PREFIX}_*" -printf '%T@ %p\n' \
        2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"

if [ -n "$PREV" ] && [ "$NEW_MD5" = "$(md5sum "$PREV" | cut -d' ' -f1)" ]; then
  # 上游当天还没更新。不重复归档，但仍然把旧文件报出去——
  # 库里可能落后于文件（比如昨天下到了、入库那步挂了），交给 sync_amv.py 判断。
  log "⚠️  内容与上次($(basename "$PREV"))完全相同，上游当天还没更新"
  RESULT_STATUS="unchanged"; RESULT_FILE="$PREV"
else
  cp -f "$SRC" "$DEST" || die "写归档文件失败: $DEST"
  # 校验真的落地了。cp 失败在 set -e 关掉时是静默的，而这一步失败
  # 会让 SKIP_IF_SAME 永远拿旧文件比对，从此再也不更新。
  [ -s "$DEST" ] || die "归档文件写完是空的: $DEST"
  ln -sfn "$DEST" "$ARCHIVE_DIR/latest.${EXT}" || log "（latest 符号链接没建上，不影响入库）"
  log "✅ 已归档: $DEST"
  RESULT_STATUS="archived"; RESULT_FILE="$DEST"
fi

# ---- 6. 清理网盘 ----
if [ "$CLEAN_PAN" = "1" ]; then
  "$PCS" rm "$PAN_WORKDIR/*" >>"$LOG_FILE" 2>&1 || true
  [ "$CLEAR_RECYCLE" = "1" ] && "$PCS" recycle delete -all >>"$LOG_FILE" 2>&1
  log "已清理网盘中转文件"
fi

# ---- 7. 清理过期归档 ----
if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
  DELN="$(find "$ARCHIVE_DIR" -maxdepth 1 -type f -name "${FILE_PREFIX}_*" \
          ! -samefile "$RESULT_FILE" -mtime "+$KEEP_DAYS" -print -delete 2>/dev/null | wc -l)"
  [ "$DELN" -gt 0 ] && log "清理了 $DELN 个超过 ${KEEP_DAYS} 天的旧文件"
fi

log "===== 结束 ($RESULT_STATUS) ====="
printf 'STATUS=%s\nFILE=%s\n' "$RESULT_STATUS" "$RESULT_FILE"
exit 0
