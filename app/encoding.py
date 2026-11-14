"""模块 1 · 编码探测。

规则：BOM → UTF-8-SIG；严格 UTF-8 解码失败 → GB18030；都失败 → 抛 EncodingError（HTTP 400）。
每次探测都保留证据：是否有 BOM、前 16 字节摘要、逐次尝试结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

UTF8_BOM = b"\xef\xbb\xbf"
SAMPLE_BYTES = 16


def sample_hex(raw: bytes) -> str:
    """前 16 字节摘要，用于在页面上证明「我们看的是同一份字节」。"""
    return raw[:SAMPLE_BYTES].hex(" ")


class EncodingError(ValueError):
    """无法用支持的编码解码；message 面向值班同学，带修复建议。"""

    def __init__(self, message: str, attempts: list[dict], sample: str) -> None:
        super().__init__(message)
        self.message = message
        self.attempts = attempts
        self.sample_hex = sample


@dataclass
class Probe:
    """探测结果 + 解码后的文本（文本不出现在 HTTP 证据里）。"""

    encoding: str
    bom: bool
    sample_hex: str
    attempts: list[dict] = field(default_factory=list)
    text: str = ""

    def evidence(self) -> dict:
        return {
            "encoding": self.encoding,
            "bom": self.bom,
            "sample_hex": self.sample_hex,
            "attempts": list(self.attempts),
        }


def probe(raw: bytes) -> Probe:
    if not raw:
        raise EncodingError(
            "文件是空的（0 字节），无法探测编码。请确认上游导出成功后再上传。",
            [],
            "",
        )

    sample = sample_hex(raw)
    attempts: list[dict] = []

    if raw.startswith(UTF8_BOM):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            attempts.append({
                "encoding": "utf-8-sig", "ok": False, "offset": exc.start,
                "detail": f"BOM 存在，但第 {exc.start} 字节起不是合法 UTF-8（{exc.reason}）",
            })
            raise EncodingError(
                "文件以 UTF-8 BOM 开头，但正文不是合法 UTF-8，多半是「BOM + GBK 内容」的混搭导出。"
                "建议：用 Excel 选择「CSV UTF-8」重存一次；或去掉 BOM 后按 GBK 重新导出。",
                attempts, sample,
            ) from exc
        attempts.append({
            "encoding": "utf-8-sig", "ok": True,
            "detail": "前 3 字节为 EF BB BF，按 UTF-8-SIG 解码成功（BOM 已剥离）",
        })
        return Probe("utf-8-sig", True, sample, attempts, text)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        attempts.append({
            "encoding": "utf-8", "ok": False, "offset": exc.start,
            "detail": f"第 {exc.start} 字节起不是合法 UTF-8（{exc.reason}）",
        })
    else:
        attempts.append({
            "encoding": "utf-8", "ok": True,
            "detail": "无 BOM，严格 UTF-8 解码成功",
        })
        return Probe("utf-8", False, sample, attempts, text)

    try:
        text = raw.decode("gb18030")
    except UnicodeDecodeError as exc:
        attempts.append({
            "encoding": "gb18030", "ok": False, "offset": exc.start,
            "detail": f"第 {exc.start} 字节起不是合法 GB18030（{exc.reason}）",
        })
        raise EncodingError(
            "既不是合法 UTF-8，也不是合法 GB18030（GBK 超集），文件可能被二次转码或已截断。"
            "建议：1) 在来源系统用「CSV UTF-8」重新导出；"
            "2) 若来源是 Windows 中文 Excel，用 iconv -f GBK -t UTF-8 先验证能整份转换；"
            "3) 检查是否用了 UTF-16/UTF-32（本工具暂不支持）。",
            attempts, sample,
        ) from exc

    attempts.append({
        "encoding": "gb18030", "ok": True,
        "detail": "严格 UTF-8 失败后按 GB18030 解码成功（判定为 GBK 系编码）",
    })
    return Probe("gb18030", False, sample, attempts, text)
