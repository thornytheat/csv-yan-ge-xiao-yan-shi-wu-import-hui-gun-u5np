"""编码探测：BOM / UTF-8 / GBK / 非法字节 / 空文件 / BOM+GBK 混搭。"""
from __future__ import annotations

import pytest

from app import encoding


def test_bom_utf8_is_detected_and_stripped():
    raw = encoding.UTF8_BOM + "site,category\n北京,电子\n".encode("utf-8")
    probe = encoding.probe(raw)
    assert probe.encoding == "utf-8-sig"
    assert probe.bom is True
    assert probe.text.startswith("site,category")
    assert "\ufeff" not in probe.text                      # BOM 已剥离
    assert probe.evidence()["bom"] is True
    assert probe.evidence()["attempts"][0]["ok"] is True


def test_plain_utf8_with_chinese():
    raw = "order_id,memo\n1001,华东仓补货\n".encode("utf-8")
    probe = encoding.probe(raw)
    assert probe.encoding == "utf-8"
    assert probe.bom is False
    assert "华东仓补货" in probe.text


def test_ascii_only_prefers_utf8_deterministically():
    probe = encoding.probe(b"site,category\nbeijing,grocery\n")
    assert probe.encoding == "utf-8"
    assert [a["encoding"] for a in probe.attempts] == ["utf-8"]


def test_gbk_falls_back_to_gb18030_with_evidence():
    raw = "site,memo\nbeijing,华东仓补货\n".encode("gb18030")
    probe = encoding.probe(raw)
    assert probe.encoding == "gb18030"
    assert "华东仓补货" in probe.text
    assert probe.attempts[0]["encoding"] == "utf-8"
    assert probe.attempts[0]["ok"] is False                # 先失败于严格 UTF-8
    assert probe.attempts[-1]["encoding"] == "gb18030"
    assert probe.attempts[-1]["ok"] is True


def test_sample_hex_is_first_16_bytes():
    raw = b"site,category,order_id,order_date\n"
    probe = encoding.probe(raw)
    assert probe.sample_hex == raw[:16].hex(" ")
    assert len(probe.sample_hex.split(" ")) == 16


def test_illegal_bytes_raise_with_suggestions():
    raw = b"site,category\n\x80\x81\xfe\xff\x00\x01,broken\n"
    with pytest.raises(encoding.EncodingError) as excinfo:
        encoding.probe(raw)
    err = excinfo.value
    assert "GB18030" in err.message
    assert "建议" in err.message
    assert [a["ok"] for a in err.attempts] == [False, False]   # utf-8 与 gb18030 都失败
    assert err.sample_hex == raw[:16].hex(" ")
    assert err.attempts[0]["offset"] is not None


def test_empty_file_is_rejected():
    with pytest.raises(encoding.EncodingError) as excinfo:
        encoding.probe(b"")
    assert "空" in excinfo.value.message


def test_bom_with_gbk_body_is_rejected_not_silently_wrong():
    raw = encoding.UTF8_BOM + "site,memo\nbeijing,华东仓\n".encode("gb18030")
    with pytest.raises(encoding.EncodingError) as excinfo:
        encoding.probe(raw)
    assert "BOM" in excinfo.value.message
    assert excinfo.value.attempts[0]["encoding"] == "utf-8-sig"


def test_gbk_sample_file_from_repo_decodes_to_120_rows():
    from app import settings
    raw = (settings.SAMPLE_DIR / "gbk_120.csv").read_bytes()
    probe = encoding.probe(raw)
    assert probe.encoding == "gb18030"
    assert len([ln for ln in probe.text.splitlines() if ln.strip()]) == 121  # 表头 + 120
    assert "华东仓" in probe.text
