"""trade_parser.py 测试 — 口语化/JSON/CSV 多格式交易输入解析"""

import json
import pytest
from modules.trade_parser import TradeParser, ParseResult, format_trade_for_review, STOCK_NAME_MAP


@pytest.fixture
def parser():
    return TradeParser()


# ==================== ParseResult 基础 ====================


class TestParseResult:
    def test_dataclass_fields(self):
        r = ParseResult(success=True, confidence=0.8, data={"a": 1}, missing_fields=[])
        assert r.success is True
        assert r.confidence == 0.8
        assert r.data == {"a": 1}
        assert r.missing_fields == []
        assert r.error_message == ""

    def test_dataclass_defaults(self):
        r = ParseResult(success=False, confidence=0, data=None, missing_fields=["x"])
        assert r.error_message == ""


# ==================== JSON 解析 ====================


class TestParseJson:
    def test_json_object_full(self, parser):
        text = json.dumps(
            {"ts_code": "600519.SH", "action": "BUY", "price": 1800.0, "quantity": 100, "trade_date": "2026-01-15"}
        )
        r = parser.parse(text)
        assert r.success is True
        assert r.confidence == 1.0
        assert r.data["ts_code"] == "600519.SH"
        assert r.data["action"] == "BUY"
        assert r.data["price"] == 1800.0
        assert r.data["quantity"] == 100
        assert r.missing_fields == []

    def test_json_array_takes_first(self, parser):
        text = json.dumps([{"code": "600519", "action": "买入", "price": 1800, "quantity": 100}])
        r = parser.parse(text)
        assert r.success is True
        assert r.data["ts_code"] == "600519.SH"

    def test_json_with_chinese_keys(self, parser):
        text = json.dumps({"股票代码": "000001", "日期": "2026-01-15", "买卖": "买入", "单价": 15.5, "股数": 1000})
        r = parser.parse(text)
        assert r.success is True
        assert r.data["ts_code"] == "000001.SZ"
        assert r.data["action"] == "BUY"
        assert r.data["price"] == 15.5

    def test_json_missing_fields_lower_confidence(self, parser):
        text = json.dumps({"ts_code": "600519.SH", "action": "BUY"})
        r = parser.parse(text)
        assert r.success is True
        assert r.confidence == 0.7
        assert "price" in r.missing_fields
        assert "quantity" in r.missing_fields

    def test_json_invalid(self, parser):
        r = parser.parse("{not valid json}")
        assert r.success is False
        assert "JSON解析失败" in r.error_message

    def test_json_code_normalization_sz(self, parser):
        text = json.dumps({"code": "000001", "action": "买", "price": 10, "quantity": 100})
        r = parser.parse(text)
        assert r.data["ts_code"] == "000001.SZ"

    def test_json_code_normalization_sh(self, parser):
        text = json.dumps({"code": "600519", "action": "买", "price": 10, "quantity": 100})
        r = parser.parse(text)
        assert r.data["ts_code"] == "600519.SH"

    def test_json_code_normalization_bj(self, parser):
        text = json.dumps({"code": "430047", "action": "买", "price": 10, "quantity": 100})
        r = parser.parse(text)
        assert r.data["ts_code"] == "430047.BJ"

    def test_json_action_standardization(self, parser):
        # 含"买"的标准化为 BUY，含"卖"的标准化为 SELL
        for action_text in ["买入", "买", "BUY"]:
            r = parser.parse(json.dumps({"action": action_text, "ts_code": "600519.SH", "price": 10, "quantity": 100}))
            assert r.data["action"] == "BUY", f"'{action_text}' should be BUY"

        for action_text in ["卖出", "卖", "SELL"]:
            r = parser.parse(json.dumps({"action": action_text, "ts_code": "600519.SH", "price": 10, "quantity": 100}))
            assert r.data["action"] == "SELL", f"'{action_text}' should be SELL"


# ==================== CSV 解析 ====================


class TestParseCsv:
    def test_csv_pipe_separated(self, parser):
        text = "股票代码|日期|买卖|单价|数量\n600519|2026-01-15|买入|1800|100"
        r = parser.parse(text)
        assert r.success is True
        assert r.data["ts_code"] == "600519.SH"
        assert r.data["action"] == "BUY"
        assert float(r.data["price"]) == 1800.0

    def test_csv_tab_separated(self, parser):
        text = "code\tdate\taction\tprice\tquantity\n600519\t2026-01-15\tBUY\t1800\t100"
        r = parser.parse(text)
        assert r.success is True
        assert r.data["ts_code"] == "600519.SH"

    def test_csv_comma_separated(self, parser):
        text = "code,date,action,price,quantity\n600519,2026-01-15,BUY,1800,100"
        r = parser.parse(text)
        assert r.success is True

    def test_csv_single_line_is_not_csv(self, parser):
        """单行不被识别为 CSV，走口语化路径"""
        text = "600519 买入 1800元 100股"
        r = parser.parse(text)
        # 走 natural 路径，不是 CSV
        assert r.success is True

    def test_csv_confidence_full(self, parser):
        text = "股票代码|日期|买卖|单价|数量\n600519|2026-01-15|买入|1800|100"
        r = parser.parse(text)
        assert r.confidence == 0.9

    def test_csv_confidence_missing(self, parser):
        text = "股票代码|日期|买卖\n600519|2026-01-15|买入"
        r = parser.parse(text)
        assert r.confidence == 0.6


# ==================== 口语化解析 ====================


class TestParseNatural:
    def test_full_natural_input(self, parser):
        text = "今天买了茅台600519，1800元，100股"
        r = parser.parse(text)
        assert r.success is True
        assert r.data["ts_code"] == "600519.SH"
        assert r.data["action"] == "BUY"
        assert r.data["price"] == 1800.0
        assert r.data["quantity"] == 100
        assert r.data["name"] == "茅台"
        assert "amount" in r.data
        assert r.data["amount"] == 180000.0

    def test_natural_stock_code_6_start(self, parser):
        r"""代码模式 [012]\d{5} 只匹配 0/1/2 开头，6 开头走名称匹配"""
        text = "买入茅台，价格1800元，100股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "600519.SH"

    def test_natural_stock_code_0_start(self, parser):
        text = "买入000001，价格15元，1000股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "000001.SZ"

    def test_natural_stock_code_3_start(self, parser):
        """3 开头走名称匹配（宁德时代）"""
        text = "买入宁德时代，价格200元，100股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "300750.SZ"

    def test_natural_stock_code_4_start(self, parser):
        """4 开头需通过名称匹配"""
        # 6 开头不在 [012] 模式里，用括号格式
        r2 = parser.parse("买入(601012) 价格20元 100股")
        assert r2.data["ts_code"] == "601012.SH"

    def test_natural_by_name(self, parser):
        text = "卖出比亚迪，价格250元，200股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "002594.SZ"
        assert r.data["action"] == "SELL"
        assert r.data["name"] == "比亚迪"

    def test_natural_date_today(self, parser):
        text = "今天买茅台600519，1800元，100股"
        r = parser.parse(text)
        from datetime import datetime

        assert r.data["trade_date"] == datetime.now().strftime("%Y-%m-%d")

    def test_natural_date_yesterday(self, parser):
        text = "昨天买茅台600519，1800元，100股"
        r = parser.parse(text)
        from datetime import datetime, timedelta

        expected = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert r.data["trade_date"] == expected

    def test_natural_date_explicit(self, parser):
        text = "2026-01-15 买入600519，1800元，100股"
        r = parser.parse(text)
        assert r.data["trade_date"] == "2026-01-15"

    def test_natural_date_slash_format(self, parser):
        text = "2026/01/15 买入600519，1800元，100股"
        r = parser.parse(text)
        assert r.data["trade_date"] == "2026-01-15"

    def test_natural_price_patterns(self, parser):
        # "X元" 格式
        r1 = parser.parse("买入600519 1800元 100股")
        assert r1.data["price"] == 1800.0

        # "价格X" 格式
        r2 = parser.parse("买入600519 价格1800 100股")
        assert r2.data["price"] == 1800.0

        # "@X" 格式
        r3 = parser.parse("买入600519 @1800 100股")
        assert r3.data["price"] == 1800.0

    def test_natural_quantity_patterns(self, parser):
        r1 = parser.parse("买入600519 1800元 100股")
        assert r1.data["quantity"] == 100

        r2 = parser.parse("买入600519 1800元 数量200")
        assert r2.data["quantity"] == 200

    def test_natural_no_action_no_code(self, parser):
        """缺关键字段 → 低置信度"""
        text = "1800元，100股"
        r = parser.parse(text)
        assert r.success is True
        assert r.confidence == 0.4
        assert "ts_code" in r.missing_fields
        assert "action" in r.missing_fields

    def test_natural_sell_direction(self, parser):
        text = "卖出600519，1800元，100股"
        r = parser.parse(text)
        assert r.data["action"] == "SELL"

    def test_natural_decimal_price(self, parser):
        text = "买入600519 18.5元 100股"
        r = parser.parse(text)
        assert r.data["price"] == 18.5

    def test_natural_confidence_complete(self, parser):
        """有 code + action + price + quantity + date → 0.85"""
        text = "今天买入000001 1800元 100股"
        r = parser.parse(text)
        assert r.confidence == 0.85

    def test_natural_confidence_partial(self, parser):
        """有 code + action 但缺 price/quantity → 0.6"""
        text = "买入000001"
        r = parser.parse(text)
        assert r.confidence == 0.6


# ==================== _map_fields ====================


class TestMapFields:
    def test_chinese_field_names(self, parser):
        mapped = parser._map_fields({"股票代码": "600519", "买卖": "买入", "单价": 100, "股数": 10})
        assert mapped["ts_code"] == "600519.SH"
        assert mapped["action"] == "BUY"
        assert mapped["price"] == 100
        assert mapped["quantity"] == 10

    def test_english_field_names(self, parser):
        mapped = parser._map_fields({"code": "000001", "action": "sell", "price": 10, "quantity": 100})
        assert mapped["ts_code"] == "000001.SZ"
        # "sell" 不含"卖"，不会被标准化为 SELL
        assert mapped["action"] == "sell"

    def test_unknown_field_passthrough(self, parser):
        mapped = parser._map_fields({"custom_field": "value"})
        assert mapped["custom_field"] == "value"


# ==================== _check_required_fields ====================


class TestCheckRequired:
    def test_all_present(self, parser):
        data = {"trade_date": "2026-01-15", "ts_code": "600519.SH", "action": "BUY", "price": 100, "quantity": 10}
        assert parser._check_required_fields(data) == []

    def test_missing_one(self, parser):
        data = {"trade_date": "2026-01-15", "ts_code": "600519.SH", "action": "BUY", "price": 100}
        missing = parser._check_required_fields(data)
        assert "quantity" in missing

    def test_empty_value_counts_as_missing(self, parser):
        data = {"trade_date": "", "ts_code": "600519.SH", "action": "BUY", "price": 100, "quantity": 10}
        missing = parser._check_required_fields(data)
        assert "trade_date" in missing


# ==================== confirm_and_fill ====================


class TestConfirmAndFill:
    def test_confirm(self, parser):
        data = {"ts_code": "600519.SH", "action": "BUY", "price": 1800, "quantity": 100}
        result = parser.confirm_and_fill(data, "对")
        assert result == data

    def test_confirm_ok(self, parser):
        data = {"ts_code": "600519.SH"}
        result = parser.confirm_and_fill(data, "ok")
        assert result == data

    def test_negate_returns_original(self, parser):
        """否定当前只返回原数据（未实现修正逻辑）"""
        data = {"ts_code": "600519.SH"}
        result = parser.confirm_and_fill(data, "不对，价格是1900")
        assert result == data


# ==================== generate_confirm_message ====================


class TestGenerateConfirm:
    def test_full_data(self, parser):
        data = {
            "trade_date": "2026-01-15",
            "ts_code": "600519.SH",
            "name": "茅台",
            "action": "BUY",
            "price": 1800,
            "quantity": 100,
            "amount": 180000,
        }
        msg = parser.generate_confirm_message(data)
        assert "2026-01-15" in msg
        assert "茅台" in msg
        assert "买入" in msg
        assert "1800" in msg
        assert "100" in msg
        assert "180000" in msg

    def test_sell_action(self, parser):
        data = {"action": "SELL", "ts_code": "600519.SH", "price": 1900, "quantity": 50}
        msg = parser.generate_confirm_message(data)
        assert "卖出" in msg

    def test_minimal_data(self, parser):
        data = {}
        msg = parser.generate_confirm_message(data)
        assert msg.startswith("确认一下：")


# ==================== format_trade_for_review ====================


class TestFormatTradeForReview:
    def test_full_trade(self):
        data = {
            "trade_date": "2026-01-15",
            "ts_code": "600519.SH",
            "name": "茅台",
            "action": "BUY",
            "price": 1800,
            "quantity": 100,
            "amount": 180000,
            "reason": "B1信号",
        }
        text = format_trade_for_review(data)
        assert "2026-01-15" in text
        assert "茅台" in text
        assert "买入" in text
        assert "1800" in text
        assert "B1信号" in text

    def test_sell_trade(self):
        data = {"action": "SELL", "ts_code": "000001.SZ", "name": "平安", "price": 15, "quantity": 1000}
        text = format_trade_for_review(data)
        assert "卖出" in text
        assert "平安" in text

    def test_no_amount_no_reason(self):
        data = {"action": "BUY", "ts_code": "600519.SH", "price": 1800, "quantity": 100}
        text = format_trade_for_review(data)
        assert "金额" not in text
        assert "原因" not in text


# ==================== STOCK_NAME_MAP ====================


class TestStockNameMap:
    def test_common_stocks_present(self):
        assert "茅台" in STOCK_NAME_MAP
        assert STOCK_NAME_MAP["茅台"] == "600519.SH"
        assert STOCK_NAME_MAP["比亚迪"] == "002594.SZ"
        assert STOCK_NAME_MAP["宁德时代"] == "300750.SZ"

    def test_name_lookup_in_natural_parse(self, parser):
        """名称映射在口语化解析中生效"""
        text = "买入招行 30元 1000股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "600036.SH"
        assert r.data["name"] == "招行"


# ==================== 边界情况 ====================


class TestEdgeCases:
    def test_empty_string(self, parser):
        r = parser.parse("")
        assert r.success is True  # natural parser always returns success=True
        assert r.confidence == 0.4

    def test_whitespace_only(self, parser):
        r = parser.parse("   ")
        assert r.success is True

    def test_very_long_input(self, parser):
        text = "买入000001 1800元 100股" + "废话" * 500
        r = parser.parse(text)
        assert r.success is True
        assert r.data["ts_code"] == "000001.SZ"

    def test_mixed_format_json_detected(self, parser):
        """JSON 优先级高于口语化，缺 trade_date → 0.7"""
        text = '{"ts_code": "600519.SH", "action": "BUY", "price": 1800, "quantity": 100}'
        r = parser.parse(text)
        assert r.confidence == 0.7  # 缺 trade_date

    def test_chinese_brackets_code(self, parser):
        text = "买入（600519）1800元 100股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "600519.SH"

    def test_english_brackets_code(self, parser):
        text = "买入(600519) 1800元 100股"
        r = parser.parse(text)
        assert r.data["ts_code"] == "600519.SH"


# ==================== 口语化解析回归（2026-09-04 修复）====================


class TestNaturalParseRegressions:
    """这一组全部来自实测踩到的错解析，每条都曾把错误数据写进 trade_records。"""

    def test_date_digits_not_taken_as_stock_code(self, parser):
        """紧凑日期 20260903 里的 "202609" 不能被当成股票代码。

        旧正则 ``[012]\\d{5}`` 无边界，会从日期里切出 6 位数字，
        于是这条记录以 ts_code="202609" 落库，日期还退回了"今天"。
        """
        r = parser.parse("20260903 买入 000001.SZ 100股 11.88元")
        assert r.data["ts_code"] == "000001.SZ"
        assert r.data["trade_date"] == "2026-09-03"
        assert r.data["quantity"] == 100
        assert r.data["price"] == 11.88

    def test_compact_date_alone(self, parser):
        r = parser.parse("20260903买入600519 100股 1800元")
        assert r.data["trade_date"] == "2026-09-03"

    def test_chinese_date_format(self, parser):
        r = parser.parse("2026年1月15日 买入600519 100股 1800元")
        assert r.data["trade_date"] == "2026-01-15"

    def test_invalid_compact_date_does_not_crash(self, parser):
        """8 位但不是合法日期（20261332）不能抛异常，也不该被当日期用"""
        r = parser.parse("买入600519 20261332 100股")
        assert r.success is True
        assert r.data["ts_code"] == "600519.SH"

    def test_bare_6_start_code(self, parser):
        """6 开头的裸代码必须能直接认出，不再依赖名称表兜底"""
        r = parser.parse("卖出600519 50股")
        assert r.data["ts_code"] == "600519.SH"
        assert r.data["action"] == "SELL"
        assert r.data["quantity"] == 50

    def test_bare_3_start_code(self, parser):
        r = parser.parse("买入300750 200股 100元")
        assert r.data["ts_code"] == "300750.SZ"

    def test_longest_name_wins(self, parser):
        """"平安银行"不能被内置别名"平安"(中国平安)劫走"""
        r = parser.parse("买入平安银行100股 价格11.88")
        assert r.data["ts_code"] == "000001.SZ"
        assert r.data["name"] == "平安银行"

    def test_explicit_code_beats_name(self, parser):
        """写明代码时名称不得覆盖它，冲突要在 error_message 里说明"""
        r = parser.parse("买入 000001.SZ 中国平安 100股 11元")
        assert r.data["ts_code"] == "000001.SZ"
        assert "对不上" in r.error_message

    def test_suffixed_code_is_kept(self, parser):
        r = parser.parse("买入000001.SZ 100股 11元")
        assert r.data["ts_code"] == "000001.SZ"

    def test_chinese_quantity(self, parser):
        """"两百股"要解析成 200，不能掉进兜底规则变成 1"""
        r = parser.parse("今天以11.9买了000001.SZ两百股")
        assert r.data["quantity"] == 200
        assert r.data["price"] == 11.9

    def test_quantity_not_taken_from_stock_code(self, parser):
        """``买了?\\s*(\\d+)`` 兜底规则不得再抓到股票代码的数字"""
        r = parser.parse("买了000001.SZ两百股")
        assert r.data["quantity"] == 200

    def test_price_with_yi_prefix(self, parser):
        r = parser.parse("买入600519 以1800 100股")
        assert r.data["price"] == 1800.0

    def test_ambiguous_bare_integer_is_not_guessed_as_price(self, parser):
        """"50股 1500" 的 1500 可能是单价也可能是金额，宁可报缺失也不猜"""
        r = parser.parse("卖出600519 50股 1500")
        assert r.data["quantity"] == 50
        assert "price" in r.missing_fields

    def test_non_stock_6_digits_rejected(self, parser):
        """不属于任何交易所号段的 6 位数字不能当成代码"""
        r = parser.parse("买入 123456 100股")
        assert "ts_code" in r.missing_fields


class TestNormalizeTsCode:
    def test_prefix_mapping(self):
        from modules.trade_parser import _normalize_ts_code

        assert _normalize_ts_code("000001") == "000001.SZ"
        assert _normalize_ts_code("300750") == "300750.SZ"
        assert _normalize_ts_code("600519") == "600519.SH"
        assert _normalize_ts_code("430123") == "430123.BJ"
        assert _normalize_ts_code("830799") == "830799.BJ"

    def test_already_suffixed_untouched(self):
        from modules.trade_parser import _normalize_ts_code

        assert _normalize_ts_code("600519.SH") == "600519.SH"

    def test_unknown_prefix_untouched(self):
        from modules.trade_parser import _normalize_ts_code

        assert _normalize_ts_code("123456") == "123456"


class TestChineseNumber:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("两百", 200),
            ("一千五", 1500),
            ("三十五", 35),
            ("一百零五", 105),
            ("一万两千", 12000),
            ("十", 10),
            ("三", 3),
        ],
    )
    def test_conversion(self, text, expected):
        from modules.trade_parser import _cn_to_int

        assert _cn_to_int(text) == expected

    def test_garbage_returns_none(self):
        from modules.trade_parser import _cn_to_int

        assert _cn_to_int("abc") is None
        assert _cn_to_int("") is None
