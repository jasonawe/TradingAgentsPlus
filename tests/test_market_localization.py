"""Auto-resolution of Chinese asset names (Tencent quote lookup + curated map).

Tests cover the symbol→Tencent-key conversion, GBK decoding, the cache, and
the fallback ordering inside localized_asset_name.  Network calls are mocked so
the suite runs offline.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from web.market_localization import (
    _CJK_RE,
    _decode_gb18030,
    _tencent_cache,
    _tencent_lookup,
    _tencent_symbol_key,
    localized_asset_name,
    tencent_asset_name,
)


# -- Symbol → Tencent key -----------------------------------------------------


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600418.SS", "sh600418"),
        ("600031.SS", "sh600031"),
        ("513880.SS", "sh513880"),
        ("300750.SZ", "sz300750"),
        ("000001.SZ", "sz000001"),
        ("00700.HK", "hk00700"),
        ("09988.HK", "hk09988"),
        ("AAPL", None),
        ("BTC-USD", None),
        ("", None),
        (None, None),
        ("sh600418", "sh600418"),
        ("600418", "sh600418"),
        ("00700", "hk00700"),
    ],
)
def test_tencent_symbol_key(symbol, expected):
    assert _tencent_symbol_key(symbol) == expected


# -- GBK decoding --------------------------------------------------------------


def test_decode_gb18030_handles_chinese():
    raw = "v_sh600418=\"1~江淮汽车~600418\"".encode("gb18030")
    decoded = _decode_gb18030(raw)
    assert "江淮汽车" in decoded
    assert _CJK_RE.search(decoded)


def test_decode_gb18030_handles_ascii():
    assert _decode_gb18030(b"hello") == "hello"


# -- localized_asset_name ordering -------------------------------------------


def test_localized_returns_provider_chinese_name_when_already_localized():
    assert localized_asset_name("600418.SS", "江淮汽车") == "江淮汽车"


def test_localized_prefers_curated_over_tencent(monkeypatch):
    # 600031 is in curated map → must not hit the network.
    def fail(symbol):
        raise AssertionError("network should not be called for curated symbols")

    monkeypatch.setattr("web.market_localization._tencent_lookup", fail)
    assert localized_asset_name("600031.SS", "Sany Heavy Industry Co.,Ltd") == "三一重工"


def test_localized_prefers_source_alias_when_mapped(monkeypatch):
    def fail(symbol):
        raise AssertionError("network should not be called when alias matches")

    monkeypatch.setattr("web.market_localization._tencent_lookup", fail)
    # cxmt corporation is in _SOURCE_NAMES_ZH alias map
    assert (
        localized_asset_name("688836.SS", "CXMT Corporation")
        == "长鑫科技"
    )


def test_localized_falls_back_to_tencent_when_needed(monkeypatch):
    monkeypatch.setattr(
        "web.market_localization._tencent_lookup",
        lambda key: "江淮汽车" if key == "sh600418" else None,
    )
    _tencent_cache.clear()
    name = localized_asset_name(
        "600418.SS", "Anhui Jianghuai Automobile Group Corp.,Ltd."
    )
    assert name == "江淮汽车"


def test_localized_returns_cleaned_english_when_remote_fails(monkeypatch):
    monkeypatch.setattr("web.market_localization._tencent_lookup", lambda key: None)
    _tencent_cache.clear()
    name = localized_asset_name("XYZ.SS", "Foobar Widgets, Co. Ltd.")
    assert name == "Foobar Widgets"


# -- Tencent cache ------------------------------------------------------------


def test_tencent_asset_name_uses_cache(monkeypatch):
    calls = {"n": 0}

    def fake(key):
        calls["n"] += 1
        return "江淮汽车"

    monkeypatch.setattr("web.market_localization._tencent_lookup", fake)
    _tencent_cache.clear()
    assert tencent_asset_name("600418.SS") == "江淮汽车"
    assert tencent_asset_name("600418.SS") == "江淮汽车"
    assert calls["n"] == 1


def test_tencent_asset_name_returns_none_for_unsupported_suffix(monkeypatch):
    def fail(key):
        raise AssertionError("should not call API for unsupported suffix")

    monkeypatch.setattr("web.market_localization._tencent_lookup", fail)
    assert tencent_asset_name("AAPL") is None
    assert tencent_asset_name("BTC-USD") is None


# -- Tencent payload parsing --------------------------------------------------


def test_tencent_lookup_returns_chinese_name_from_payload():
    payload = 'v_sh600418="1~江淮汽车~600418~20.48"'.encode("gb18030")
    with patch("web.market_localization.requests.get") as gget:
        gget.return_value.content = payload
        gget.return_value.raise_for_status = lambda: None
        assert _tencent_lookup("sh600418") == "江淮汽车"


def test_tencent_lookup_rejects_payload_without_cjk():
    payload = b'v_sh600418="1~~600418~20.48"'
    with patch("web.market_localization.requests.get") as gget:
        gget.return_value.content = payload
        gget.return_value.raise_for_status = lambda: None
        assert _tencent_lookup("sh600418") is None


def test_tencent_lookup_returns_none_on_network_error():
    import requests

    with patch(
        "web.market_localization.requests.get",
        side_effect=requests.RequestException("boom"),
    ):
        assert _tencent_lookup("sh600418") is None
