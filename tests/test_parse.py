"""文本提取：清洗规则 + PDF/DOCX 解析 + 异常收口。

parser 是纯函数模块（输入路径、输出字符串），所以这一组完全不碰数据库和 HTTP。
只在需要真文件的地方落盘到 tmp_path，用完由 pytest 自动清理。

PDF 用 reportlab 现场生成：一份真实的、带文字层的 PDF 比任何 mock 都能证明
「pdfplumber 这条路是通的」。中文字体用 reportlab 内置的 CID 字体 STSong-Light，
不需要往仓库里塞字体文件。
"""

from pathlib import Path

import pytest
from docx import Document
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.services.parser import _clean, extract_text, parse_docx, parse_pdf


def _write_docx(path: Path, paragraphs: list[str], rows: list[list[str]] | None = None) -> Path:
    document = Document()
    for line in paragraphs:
        document.add_paragraph(line)
    if rows:
        table = document.add_table(rows=len(rows), cols=len(rows[0]))
        for row_index, row in enumerate(rows):
            for cell_index, value in enumerate(row):
                table.cell(row_index, cell_index).text = value
    document.save(path)
    return path


def _write_pdf(path: Path, lines: list[str]) -> Path:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path))
    pdf.setFont("STSong-Light", 14)
    y = 760
    for line in lines:
        pdf.drawString(60, y, line)
        y -= 24
    pdf.save()
    return path


# ---------------------------------------------------------------- 清洗


def test_clean_removes_zero_width_and_extra_blank_lines() -> None:
    """从网页复制的内容常带零宽字符：肉眼看不见，但会占长度、干扰模型。

    三个以上连续换行压成两个 —— PDF 每页末尾往往拖一堆空行，
    不清洗会白白吃掉上下文额度。
    """
    assert _clean("简\u200b历\r\n\r\n\r\n\r\n内容\ufeff") == "简历\n\n内容"


def test_clean_strips_trailing_spaces() -> None:
    assert _clean("第一行   \n第二行\t \n") == "第一行\n第二行"


def test_clean_keeps_single_blank_line() -> None:
    """只压 3 个以上的换行。段落之间那一个空行是有意义的分隔，不能一起吃掉。"""
    assert _clean("A\n\nB") == "A\n\nB"


# ---------------------------------------------------------------- DOCX


def test_parse_docx_reads_paragraphs_and_tables(tmp_path: Path) -> None:
    """简历模板极爱用表格排版，只读 paragraphs 会漏掉大半内容，而且不报错。

    漏了不会崩，只会让分析结果莫名其妙地差 —— 这种问题最难查，所以专门钉一条。
    """
    path = _write_docx(
        tmp_path / "resume.docx",
        ["张三", "熟悉 Python"],
        [["时间", "公司"], ["2020-2024", "某某科技"]],
    )

    text = parse_docx(path)

    assert "张三" in text
    assert "熟悉 Python" in text
    # 表格行用 | 连接，让模型能看出这是表格而不是普通段落
    assert "时间 | 公司" in text
    assert "2020-2024 | 某某科技" in text


def test_parse_docx_skips_empty_paragraphs(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "resume.docx", ["有内容", "   ", ""])

    assert parse_docx(path) == "有内容"


# ---------------------------------------------------------------- PDF


def test_parse_pdf_extracts_text_layer(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "resume.pdf", ["张三 本科", "熟悉 Python 与 FastAPI"])

    text = parse_pdf(path)

    assert "张三" in text
    assert "Python" in text


def test_parse_pdf_returns_empty_string_for_blank_page(tmp_path: Path) -> None:
    """扫描件的文字层是空的，parse_pdf 返回空串（由 extract_text 判定并报错）。

    两层职责分开：parse_pdf 只管「抽」，抽不到不抛 —— 它不知道调用方
    是要报错还是要兜底；判定放在 extract_text。
    """
    path = _write_pdf(tmp_path / "blank.pdf", [])

    assert parse_pdf(path) == ""


# ---------------------------------------------------------------- extract_text 的分发与异常


def test_extract_text_accepts_uppercase_suffix(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "resume.docx", ["正文"])

    assert extract_text(path, ".DOCX") == "正文"


def test_extract_text_rejects_unsupported_suffix(tmp_path: Path) -> None:
    """老格式 .doc 是二进制复合文档，python-docx 读不了，明确拒绝好过含糊报错。"""
    path = tmp_path / "resume.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0")

    with pytest.raises(ValueError, match="不支持的文件类型"):
        extract_text(path, ".doc")


def test_extract_text_rejects_image_only_pdf(tmp_path: Path) -> None:
    """抽不到文字时必须抛错，不能返回空串。

    空串会一路流进数据库和模型，变成一条查不出原因的坏记录；
    抛错才能在上传接口当场告诉用户「请上传带文字层的版本」。
    """
    path = _write_pdf(tmp_path / "scan.pdf", [])

    with pytest.raises(ValueError, match="未能从文件中提取到文字"):
        extract_text(path, ".pdf")


def test_extract_text_wraps_corrupt_docx(tmp_path: Path) -> None:
    """python-docx 对坏 zip 会抛各种底层异常（BadZipFile 等）。

    extract_text 把它们统一收口成 ValueError，路由层只需要处理一种异常。
    """
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK\x03\x04" + b"garbage" * 20)

    with pytest.raises(ValueError, match="文件解析失败"):
        extract_text(path, ".docx")
