"""
随堂测试解析器
支持口语化、JSON、CSV等多种格式的解析
"""

import re
from datetime import datetime, timedelta
from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class ParseResult:
    """解析结果"""

    success: bool
    confidence: float  # 0-1 置信度
    data: dict[str, Any] | None
    missing_fields: list  # 缺失的字段
    error_message: str = ""


# 股票名称到代码的映射（常见股票）
STOCK_NAME_MAP = {
    "茅台": "600519.SH",
    "贵州茅台": "600519.SH",
    # "平安"作为简称指中国平安，但"平安银行"是另一只票(000001.SZ)。
    # 两条都列出来，长名优先的匹配规则才能在读不到 stock_basic 时也判对——
    # 只留"平安"的话，"买入平安银行"会被记到中国平安头上。
    "平安": "601318.SH",
    "中国平安": "601318.SH",
    "平安银行": "000001.SZ",
    "万科": "000002.SZ",
    "宁德": "300750.SZ",
    "宁德时代": "300750.SZ",
    "隆基": "601012.SH",
    "隆基绿能": "601012.SH",
    "比亚迪": "002594.SZ",
    "招行": "600036.SH",
    "招商银行": "600036.SH",
    "五粮液": "000858.SZ",
    "海康": "002415.SZ",
    "海康威视": "002415.SZ",
}


# ==================== 代码 / 名称 / 中文数字 ====================

# 交易所前缀。旧实现只用 ``[012]\d{5}`` 抓代码，带来两个方向的错：
# 6/3 开头的票(600519、300750)压根抓不到，而"20260903"这种日期里的
# "202609"反倒被当成代码写进库。这里改成"先认带后缀的，再认独立的 6 位数字",
# 并且用前缀白名单挡掉不是股票的 6 位数。
_EXCHANGE_BY_PREFIX = {
    "0": "SZ",
    "2": "SZ",  # 深市 B 股 200xxx
    "3": "SZ",
    "4": "BJ",
    "6": "SH",
    "8": "BJ",
    "9": "SH",  # 沪市 B 股 900xxx
}

_RE_CODE_SUFFIXED = re.compile(r"(?<!\d)(\d{6})\s*\.\s*(SH|SZ|BJ)\b", re.IGNORECASE)
_RE_CODE_BRACKETED = re.compile(r"[（(](\d{6})[)）]")
_RE_CODE_BARE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_RE_DATE_COMPACT = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_RE_DATE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_RE_CN_NUMBER = re.compile(r"([零一二两三四五六七八九十百千万]+)\s*(?:股|手)")

# 名称→代码索引按库缓存：进程内只查一次 stock_basic，测试用的临时空库
# 查不到就退回内置别名表。
_NAME_INDEX_CACHE: dict[str, list[tuple[str, str]]] = {}


def _normalize_ts_code(code: str) -> str:
    """把裸 6 位代码补上交易所后缀；已带后缀或无法识别的原样返回。"""
    code = str(code).strip().upper()
    if len(code) != 6 or not code.isdigit():
        return code
    suffix = _EXCHANGE_BY_PREFIX.get(code[0])
    return f"{code}.{suffix}" if suffix else code


def _cn_to_int(text: str) -> int | None:
    r"""把"两百""一千五""三十五""一百零五"转成整数，认不出返回 None。

    旧实现没有这一步，"两百股"会掉进 ``买了?\s*(\d+)`` 兜底分支，
    把股票代码的数字当成数量，200 股变成 1 股。

    末位简写要按最后一个单位降一级补：一千五=1500、两百五=250、三十五=35；
    但"零"出现过就说明是补位读法，一百零五=105 而不是 150。
    """
    total = 0
    current = 0
    last_unit = 1
    zero_seen = False
    seen = False
    for ch in text:
        if ch == "零":
            zero_seen = True
            seen = True
            continue
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total = (total + (current or 1)) * unit
            else:
                total += (current or 1) * unit
            last_unit = unit
            current = 0
            zero_seen = False
            seen = True
        else:
            return None
    if not seen:
        return None
    if current and last_unit >= 10 and not zero_seen:
        return total + current * (last_unit // 10)
    return total + current


def _load_name_index() -> list[tuple[str, str]]:
    """名称→代码索引，按名称长度降序。

    长名优先是关键：内置别名表里有"平安"→601318.SH(中国平安)，
    输入"平安银行"时若按短名先命中，就会把 000001.SZ 的交易记到中国平安头上。
    """
    from .database import get_db_path

    key = str(get_db_path())
    cached = _NAME_INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    pairs: dict[str, str] = dict(STOCK_NAME_MAP)
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{key}?mode=ro", uri=True)
        try:
            for name, ts_code in conn.execute("SELECT name, ts_code FROM stock_basic WHERE name IS NOT NULL"):
                if name and ts_code:
                    pairs.setdefault(str(name).strip(), str(ts_code).strip())
        finally:
            conn.close()
    except Exception:
        # 库不存在/表没建（测试用临时空库就是这种情况）→ 只用内置别名表
        pass

    index = sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True)
    _NAME_INDEX_CACHE[key] = index
    return index


def _match_stock_name(text: str) -> tuple[str, str] | None:
    """在文本里找最长的股票名/别名，返回 (名称, ts_code)。"""
    for name, code in _load_name_index():
        if name and name in text:
            return name, code
    return None


class TradeParser:
    """随堂测试解析器"""

    def __init__(self):
        self.name_to_code = STOCK_NAME_MAP

    def parse(self, text: str) -> ParseResult:
        """
        解析用户输入的交易记录

        Args:
            text: 用户输入的文字

        Returns:
            ParseResult: 解析结果
        """
        # 优先级1: JSON格式
        if self._is_json(text):
            return self._parse_json(text)

        # 优先级2: CSV/表格格式
        if self._is_csv(text):
            return self._parse_csv(text)

        # 优先级3: 口语化描述（最高优先级）
        return self._parse_natural(text)

    def _is_json(self, text: str) -> bool:
        """判断是否为JSON格式"""
        text = text.strip()
        return (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))

    def _is_csv(self, text: str) -> bool:
        """判断是否为CSV/表格格式"""
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return False

        # 检查是否有明显的分隔符
        for sep in ["|", "\t", ","]:
            if sep in lines[0] and sep in lines[1]:
                return True
        return False

    def _parse_json(self, text: str) -> ParseResult:
        """解析JSON格式"""
        import json

        try:
            data = json.loads(text)
            if isinstance(data, list):
                data = data[0]  # 取第一个元素

            # 映射字段
            mapped = self._map_fields(data)

            # 检查必填字段
            missing = self._check_required_fields(mapped)
            confidence = 1.0 if not missing else 0.7

            return ParseResult(success=True, confidence=confidence, data=mapped, missing_fields=missing)
        except json.JSONDecodeError as e:
            return ParseResult(
                success=False, confidence=0, data=None, missing_fields=[], error_message=f"JSON解析失败: {str(e)}"
            )

    def _parse_csv(self, text: str) -> ParseResult:
        """解析CSV/表格格式"""
        try:
            lines = [line.strip() for line in text.strip().split("\n") if line.strip()]

            # 确定分隔符
            sep = "|"
            if "\t" in lines[0]:
                sep = "\t"
            elif "," in lines[0]:
                sep = ","

            # 解析标题行
            headers = [h.strip() for h in lines[0].split(sep)]

            # 解析数据行（取第一行）
            values = [v.strip() for v in lines[1].split(sep)]

            data = dict(zip(headers, values))
            mapped = self._map_fields(data)

            missing = self._check_required_fields(mapped)
            confidence = 0.9 if not missing else 0.6

            return ParseResult(success=True, confidence=confidence, data=mapped, missing_fields=missing)
        except Exception as e:
            return ParseResult(
                success=False, confidence=0, data=None, missing_fields=[], error_message=f"CSV解析失败: {str(e)}"
            )

    def _parse_natural(self, text: str) -> ParseResult:
        """解析口语化描述（最高优先级）"""
        data: dict[str, Any] = {}
        missing: list[str] = []
        errors: list[str] = []

        # 日期提取
        date_patterns = [
            r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            r"(\d{1,2}[月/-]\d{1,2}[日/-]?)",
            r"今天|昨天|前天|前日",
            r"今儿|昨儿",
        ]

        today = datetime.now()
        date_str = None
        date_span: tuple[int, int] | None = None

        # 紧凑写法 20260903 / 2026年9月3日 先单独认一遍：这两种是券商流水里
        # 最常见的格式，旧版一个都不认，日期默默变成"今天"，而 20260903 里的
        # "202609" 还会被后面的代码正则抓走当成股票代码。
        m = _RE_DATE_CN.search(text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                date_str = datetime(y, mo, d).strftime("%Y-%m-%d")
                date_span = m.span()
            except ValueError:
                errors.append(f"日期不合法: {m.group(0)}")
        if not date_str:
            for m in _RE_DATE_COMPACT.finditer(text):
                raw = m.group(1)
                try:
                    date_str = datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
                    date_span = m.span()
                    break
                except ValueError:
                    continue  # 8 位但不是日期（比如金额），留给后面的规则

        for pattern in [] if date_str else date_patterns:
            match = re.search(pattern, text)
            if match:
                if match.groups():
                    date_text = match.group(1)
                else:
                    date_text = match.group(0)
                if "今天" in date_text or "今儿" in text:
                    date_str = today.strftime("%Y-%m-%d")
                elif "昨天" in date_text or "昨儿" in text:
                    date_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                elif "前天" in date_text or "前日" in text:
                    date_str = (today - timedelta(days=2)).strftime("%Y-%m-%d")
                elif "-" in date_text or "/" in date_text:
                    if len(date_text) == 10:  # yyyy-mm-dd
                        date_str = date_text.replace("/", "-")
                    else:  # mm-dd 或 m-d
                        parts = re.split(r"[-/]", date_text)
                        if len(parts) == 2:
                            date_str = f"{today.year}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                date_span = match.span()
                break

        if date_str:
            data["trade_date"] = date_str
        else:
            missing.append("trade_date")
            data["trade_date"] = today.strftime("%Y-%m-%d")  # 默认今天

        # 股票代码提取：带后缀 > 括号内 > 独立 6 位数字。
        # 三条都用 (?<!\d)...(?!\d) 卡边界，"20260903" 不会再被切出 "202609"。
        ts_code = None
        code_span: tuple[int, int] | None = None
        for regex in (_RE_CODE_SUFFIXED, _RE_CODE_BRACKETED, _RE_CODE_BARE):
            for m in regex.finditer(text):
                # 落在日期里的数字不是代码
                if date_span and m.start(1) >= date_span[0] and m.end(1) <= date_span[1]:
                    continue
                digits = m.group(1)
                if regex is _RE_CODE_SUFFIXED:
                    ts_code = f"{digits}.{m.group(2).upper()}"
                else:
                    if digits[0] not in _EXCHANGE_BY_PREFIX:
                        continue  # 不是任何交易所的号段，多半是金额/编号
                    ts_code = _normalize_ts_code(digits)
                code_span = m.span()
                break
            if ts_code:
                break

        # 名称匹配独立进行：名字要记进 data["name"]，但**不能顶掉**已经写明的代码。
        # 旧实现无条件覆盖，于是"买入 000001.SZ 平安银行"会被记成中国平安。
        matched = _match_stock_name(text)
        if matched:
            name, name_code = matched
            data.setdefault("name", name)
            if not ts_code:
                ts_code = name_code
            elif ts_code != name_code:
                errors.append(f"代码 {ts_code} 与名称 {name}({name_code}) 对不上，已按代码为准")

        if ts_code:
            data["ts_code"] = ts_code
        else:
            missing.append("ts_code")

        # 价格和数量在"抠掉代码与日期"的文本上找：这两段全是数字，
        # 留在原文里会被 ``买了?\s*(\d+)`` 之类的兜底规则当成数量或价格。
        masked = list(text)
        for span in (code_span, date_span):
            if span:
                for i in range(span[0], span[1]):
                    masked[i] = " "
        masked_text = "".join(masked)

        # 交易方向
        action = None
        if "买" in text:
            action = "BUY"
            data["action"] = "BUY"
        elif "卖" in text:
            action = "SELL"
            data["action"] = "SELL"

        if not action:
            missing.append("action")

        # 价格提取
        price_patterns = [
            r"(\d+(?:\.\d{1,3})?)\s*(?:元|块)",
            r"价格?[是为]*\s*(\d+(?:\.\d{1,3})?)",
            r"@\s*(\d+(?:\.\d{1,3})?)",
            # "以11.9买" / "按11.9" —— 口语里最常见的报价说法，旧版不认
            r"(?:以|按|@)\s*(\d+(?:\.\d{1,3})?)",
            # 兜底：带小数的裸数字。数量是整数，代码和日期又已被遮蔽，
            # 剩下的小数基本只可能是价格。整数不进这条，避免把金额当单价。
            r"(?<![\d.])(\d+\.\d{1,3})(?![\d.])",
        ]

        price = None
        for pattern in price_patterns:
            match = re.search(pattern, masked_text)
            if match:
                price = float(match.group(1))
                break

        if price:
            data["price"] = price
        else:
            missing.append("price")

        # 数量提取
        qty_patterns = [
            r"(\d+)\s*(?:股|手)",
            r"数量\s*(\d+)",
            r"买了?\s*(\d+)",
            r"卖[出]?\s*(\d+)",
        ]

        quantity = None
        cn_match = _RE_CN_NUMBER.search(masked_text)
        if cn_match:
            quantity = _cn_to_int(cn_match.group(1))

        if quantity is None:
            for pattern in qty_patterns:
                match = re.search(pattern, masked_text)
                if match:
                    quantity = int(match.group(1))
                    break

        if quantity:
            data["quantity"] = quantity
        else:
            missing.append("quantity")

        # 计算金额
        if price and quantity:
            data["amount"] = round(price * quantity, 2)

        # 置信度计算
        if not data.get("ts_code") or not data.get("action"):
            confidence = 0.4
        elif missing:
            confidence = 0.6
        else:
            confidence = 0.85  # 口语化总有不确定性

        return ParseResult(
            success=True,
            confidence=confidence,
            data=data if data else None,
            missing_fields=missing,
            error_message=",".join(errors) if errors else "",
        )

    def _map_fields(self, data: dict) -> dict:
        """映射字段名到标准格式"""
        field_mapping = {
            "code": "ts_code",
            "股票代码": "ts_code",
            "date": "trade_date",
            "日期": "trade_date",
            "time": "trade_date",
            "action": "action",
            "type": "action",
            "买卖": "action",
            "买入": "action",
            "卖出": "action",
            "price": "price",
            "单价": "price",
            "成交价": "price",
            "quantity": "quantity",
            "num": "quantity",
            "数量": "quantity",
            "股数": "quantity",
            "股": "quantity",
            "amount": "amount",
            "金额": "amount",
            "total": "amount",
            "name": "name",
            "股票名称": "name",
            "证券名称": "name",
        }

        mapped = {}
        for key, value in data.items():
            mapped_key = field_mapping.get(key, key)
            mapped[mapped_key] = value

        # 标准化 action
        if "action" in mapped:
            action = str(mapped["action"]).upper()
            if "买" in action:
                mapped["action"] = "BUY"
            elif "卖" in action:
                mapped["action"] = "SELL"

        # 标准化 ts_code 格式（与口语化路径共用一份前缀表）
        if "ts_code" in mapped:
            mapped["ts_code"] = _normalize_ts_code(str(mapped["ts_code"]))

        return mapped

    def _check_required_fields(self, data: dict) -> list:
        """检查必填字段"""
        required = ["trade_date", "ts_code", "action", "price", "quantity"]
        missing = []

        for field in required:
            if field not in data or not data[field]:
                missing.append(field)

        return missing

    def confirm_and_fill(self, data: dict, user_response: str) -> dict:
        """
        根据用户的确认/修正信息更新数据

        Args:
            data: 当前数据
            user_response: 用户回复

        Returns:
            更新后的数据
        """
        # 确认词
        confirm_words = ["对", "是的", "正确", "嗯", "好", "ok", "confirm"]
        # 否定词

        response = user_response.strip().lower()

        # 如果用户确认
        if any(w in response for w in confirm_words):
            return data

        # 如果用户否定，尝试从回复中提取修正值
        for key in data.keys():
            if key in user_response:
                # 简单处理：假设用户输入了修正值
                pass

        return data

    def generate_confirm_message(self, data: dict) -> str:
        """生成确认消息"""
        lines = []

        if "trade_date" in data:
            lines.append(f"日期: {data['trade_date']}")
        if "ts_code" in data:
            name = data.get("name", data["ts_code"])
            lines.append(f"股票: {name} ({data['ts_code']})")
        if "action" in data:
            action_text = "买入" if data["action"] == "BUY" else "卖出"
            lines.append(f"方向: {action_text}")
        if "price" in data:
            lines.append(f"价格: {data['price']}元")
        if "quantity" in data:
            lines.append(f"数量: {data['quantity']}股")
        if "amount" in data:
            lines.append(f"金额: {data['amount']}元")

        return "确认一下：" + "，".join(lines)


def format_trade_for_review(data: dict) -> str:
    """格式化交易数据用于Z哥点评"""
    action_text = "买入" if data.get("action") == "BUY" else "卖出"
    name = data.get("name", data.get("ts_code", ""))
    ts_code = data.get("ts_code", "")

    lines = [
        "📋 交易记录确认",
        "",
        f"📅 日期: {data.get('trade_date', '未设置')}",
        f"📈 股票: {name} ({ts_code})",
        f"📊 方向: {action_text}",
        f"💰 价格: {data.get('price', '?')}元",
        f"🔢 数量: {data.get('quantity', '?')}股",
    ]

    if "amount" in data:
        lines.append(f"💵 金额: {data['amount']}元")

    if "reason" in data and data["reason"]:
        lines.append(f"📝 原因: {data['reason']}")

    return "\n".join(lines)
