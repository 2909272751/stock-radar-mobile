import asyncio

from backend.app.quote_provider import DualQuoteProvider
from backend.app.schemas import InstrumentType, QuoteTarget


SINA_LINE = (
    'var hq_str_sh600000="浦发银行,10.190,10.210,10.400,10.440,10.120,10.390,10.400,'
    '76157313,786507841.000,225000,10.390,46000,10.380,29400,10.370,26300,10.360,'
    '26200,10.350,52250,10.400,145362,10.410,499441,10.420,520662,10.430,687817,10.440,'
    '2026-11-13,15:34:59,00";'
)

TENCENT_LINE = (
    'v_sh600000="1~浦发银行~600000~10.40~10.21~10.19~761573~463721~297852~10.39~2250~'
    '10.38~460~10.37~294~10.36~263~10.35~262~10.40~523~10.41~1454~10.42~4994~10.43~5207~10.44~6878~'
    '~20261113161444~0.19~1.86~'
    '10.44~10.12~10.40/761573/786507841~761573~78651~0.23~6.69~~10.44~10.12~3.13~'
    '3463.81~3463.81~0.46~11.23~9.19~1.45~-15527~10.33~6.40~6.93~~~-0.01~78650.7841~'
    '12.8960~124~   A~GP-A~-13.48~3.69~4.04~6.17~0.50~12.06~8.07~3.79~6.89~13.29~"'
)


def make_target() -> QuoteTarget:
    return QuoteTarget(code="600000", instrument_type=InstrumentType.STOCK)


def test_tencent_quote_is_parsed():
    provider = DualQuoteProvider()
    client = FakeClient(TENCENT_LINE.encode("gbk"))
    snapshots = asyncio.run(provider._fetch_tencent_quotes_with_client(client, [make_target()]))
    quote = snapshots["stock:600000.SH"]
    assert quote.name == "浦发银行"
    assert quote.last_price == 10.40
    assert quote.prev_close == 10.21
    assert quote.change_pct == 1.86
    assert quote.timestamp == "2026-11-13 16:14:44"
    assert quote.source == "tencent"


def test_sina_quote_uses_sina_referer_and_parses():
    provider = DualQuoteProvider()
    client = FakeClient(SINA_LINE.encode("gbk"))
    snapshots = asyncio.run(provider._fetch_sina_quotes_with_client(client, [make_target()]))
    quote = snapshots["stock:600000.SH"]
    assert client.last_headers["Referer"] == "https://finance.sina.com.cn/"
    assert quote.name == "浦发银行"
    assert quote.last_price == 10.40
    assert quote.prev_close == 10.21


def test_search_response_decodes_chinese_name_and_skips_funds():
    text = (
        'v_hint="sh~600000~\\u6d66\\u53d1\\u94f6\\u884c~pfyh~GP-A'
        '^jj~000001~\\u534e\\u590f\\u6210\\u957f\\u6df7\\u5408~hxczhh~KJ'
        '^sh~000001~\\u4e0a\\u8bc1\\u6307\\u6570~szzs~ZS"'
    )
    results = DualQuoteProvider._parse_search_response(text)
    assert [item["code"] for item in results] == ["600000.SH", "000001.SH"]
    assert results[0]["name"] == "浦发银行"
    assert results[0]["instrument_type"] == "stock"
    assert results[1]["instrument_type"] == "index"


def test_yearly_bars_are_aggregated_from_monthly():
    bars = [
        type("Bar", (), {"time": f"2024-{index:02d}", "open": 10 + index, "close": 10 + index + 1, "high": 20 + index, "low": 5 + index, "volume": 100})()
        for index in range(1, 13)
    ]
    aggregated = DualQuoteProvider._aggregate_bars(bars, 12)
    assert len(aggregated) == 1
    assert aggregated[0].open == 11
    assert aggregated[0].close == 23
    assert aggregated[0].high == 32
    assert aggregated[0].low == 6
    assert aggregated[0].volume == 1200


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.last_headers: dict[str, str] = {}

    async def get(self, _url: str, headers: dict[str, str] | None = None) -> FakeResponse:
        self.last_headers = headers or {}
        return FakeResponse(self.content)
