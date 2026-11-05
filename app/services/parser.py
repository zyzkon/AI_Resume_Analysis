"""简历文本提取。

这个模块刻意保持"纯"：输入一个文件路径，输出一段文本，
不碰数据库、不碰 HTTP、不读配置。这样它可以脱离整个应用单独调用，
也可以被 Celery worker 直接复用。

只支持两种格式：
- .pdf  用 pdfplumber（纯 Python，不依赖 poppler 之类的外部命令）
- .docx 用 python-docx（本质是个 zip 包，读 xml）

老格式 .doc 不支持 —— 它是二进制复合文档，python-docx 读不了，
需要 LibreOffice 之类的命令行工具转换，为这点需求不值得引入。
"""

import re
from pathlib import Path

import pdfplumber
from docx import Document

# 零宽字符：从网页复制的内容常带这些，肉眼看不见但会占长度、干扰模型理解
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\u00a0]")
# 三个以上连续换行压成两个。PDF 每页末尾常有大量空行，不清洗会占满上下文
_MULTI_BLANK = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    """统一换行符、去掉行尾空格、压缩空行。"""
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _MULTI_BLANK.sub("\n\n", text).strip()


def parse_pdf(path: Path) -> str:
    """逐页抽取 PDF 文字层。

    注意：这里读的是"文字层"，不是图像。扫描件（拍照/复印后转的 PDF）
    文字层是空的，抽出来是空字符串——这种情况要由调用方判定并给出明确提示，
    不能让它变成一条 raw_text 为空的记录，否则后面的 AI 分析会拿到空白输入。
    """
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
    return _clean("\n".join(parts))


def parse_docx(path: Path) -> str:
    """抽取 Word 正文。

    必须同时处理段落和表格。简历模板极爱用表格排版——
    工作经历、教育背景经常塞在表格里，只读 doc.paragraphs 会漏掉大半内容，
    而且不会报错，只是分析结果莫名其妙地差。
    """
    doc = Document(path)

    parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]

    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                # 用 | 分隔同一个单元格行，模型能看出这是表格结构
                parts.append(" | ".join(cells))

    return _clean("\n".join(parts))


def extract_text(path: Path, suffix: str) -> str:
    """按扩展名分发，返回清洗后的纯文本。

    抽不出内容时抛 ValueError，而不是返回空字符串。
    空字符串会一路流到数据库和 AI 分析，变成一个查不出原因的坏结果；
    抛异常能在上传接口就把问题拦住，直接告诉用户"这份文件读不出文字"。
    """
    key = suffix.lower().lstrip(".")

    try:
        if key == "pdf":
            text = parse_pdf(path)
        elif key == "docx":
            text = parse_docx(path)
        else:
            raise ValueError(f"不支持的文件类型：.{key}")
    except ValueError:
        raise
    except Exception as exc:
        # pdfplumber 遇到加密文件、损坏文件会抛各种底层异常，
        # 统一收口成 ValueError，让路由层只需要处理一种异常
        raise ValueError(f"文件解析失败：{exc}") from exc

    if not text:
        raise ValueError(
            "未能从文件中提取到文字。如果是扫描件或图片版 PDF，请上传带文字层的版本。"
        )

    return text
