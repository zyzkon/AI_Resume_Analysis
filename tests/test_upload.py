"""上传接口的五道关，以及简历的列表 / 详情 / 删除。

上传是整个项目攻击面最大的地方（用户直接控制文件名、内容和大小），
所以每个用例都对应一条具体的攻击或误用：

| 用例 | 挡掉的情况 |
| --- | --- |
| 扩展名 | 传 .exe / .txt |
| 大小 | 塞一个几个 G 的文件把内存打挂 |
| 魔数 | 把 .exe 改名成 .pdf |
| 解析 | 扫描件、损坏文件、加密 PDF |
| 正文长度 | 超过 MySQL TEXT 的余量 |

后两道还会额外断言「磁盘上没留下孤儿文件」—— 落盘在解析之前，
任何一步失败都必须把刚写的文件删掉，否则 uploads/ 会越堆越多。
"""

import io
from collections.abc import Iterator

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models.analysis import Analysis
from app.models.resume import Resume
from app.models.user import User

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(text: str) -> bytes:
    """生成一份真正能被 python-docx 解析的 docx。

    不用手工拼 PK 头糊弄魔数校验：要覆盖「上传 -> 解析 -> 存库」整条路径，
    文件必须是合法的 docx，否则测出来的是魔数校验，不是上传流程。
    """
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _upload_dir_files() -> set[str]:
    return {path.name for path in settings.UPLOAD_DIR.glob("*") if path.is_file()}


@pytest.fixture
def uploads() -> Iterator[set[str]]:
    """记录上传目录的初始状态，用例结束后清掉本次新增的文件。

    清理放在 yield 之后（也就是断言之后），所以它不会掩盖「接口漏删文件」——
    断言比较的是用例执行过程中的目录快照。
    """
    before = _upload_dir_files()
    yield before
    for name in _upload_dir_files() - before:
        (settings.UPLOAD_DIR / name).unlink(missing_ok=True)


# ---------------------------------------------------------------- 正常路径


def test_upload_docx_extracts_text(
    client: TestClient, auth_headers: dict[str, str], uploads: set[str]
) -> None:
    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("简历.docx", _docx_bytes("张三 本科 熟悉 Python"), DOCX_MIME)},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "简历.docx"
    assert body["char_count"] > 0
    # 文件名被 UUID 替换：原名里可能有 ../ 或和别人的文件重名
    assert len(_upload_dir_files() - uploads) == 1

    detail = client.get(f"/resumes/{body['id']}", headers=auth_headers)
    assert "张三" in detail.json()["raw_text"]


# ---------------------------------------------------------------- 五道关


def test_upload_rejects_unsupported_suffix(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("简历.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 415
    assert ".txt" in response.json()["detail"]


def test_upload_rejects_oversized_file(
    client: TestClient, auth_headers: dict[str, str], uploads: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """把上限调到 1MB 再传 1MB+，比真造一个 5MB 文件快得多。

    这条同时守着「不能 await file.read() 一把梭」：分块读、边读边累加，
    超限立刻中断。真一把梭的话，内存早就被撑爆了，测不出来只能靠事故发生。
    """
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    oversized = b"PK\x03\x04" + b"0" * (1024 * 1024)

    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("big.docx", oversized, DOCX_MIME)},
    )

    assert response.status_code == 413
    assert "1MB" in response.json()["detail"]
    # 大小校验在落盘之前就中断了，磁盘上不该有任何东西
    assert _upload_dir_files() == uploads


def test_upload_rejects_renamed_executable(
    client: TestClient, auth_headers: dict[str, str], uploads: set[str]
) -> None:
    """MZ 是 Windows PE 文件头。扩展名说 PDF，内容不是 —— 只有魔数能拦住。"""
    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("简历.pdf", b"MZ\x90\x00" + b"0" * 64, "application/pdf")},
    )

    assert response.status_code == 415
    assert "文件内容与扩展名不符" in response.json()["detail"]
    assert _upload_dir_files() == uploads


def test_upload_rejects_empty_file(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("简历.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "文件是空的"


def test_upload_removes_file_when_parse_fails(
    client: TestClient,
    auth_headers: dict[str, str],
    uploads: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析失败时磁盘上不能留孤儿文件。

    这里 monkeypatch 解析器而不是造一个坏 docx：要测的是路由的清理逻辑
    （except ValueError -> _remove_file -> 400），解析器本身在 test_parse.py 里单独测。
    """
    def _boom(path, suffix):
        raise ValueError("未能从文件中提取到文字")

    monkeypatch.setattr("app.api.routes.resumes.extract_text", _boom)

    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("简历.docx", b"PK\x03\x04" + b"0" * 64, DOCX_MIME)},
    )

    assert response.status_code == 400
    assert "未能从文件中提取到文字" in response.json()["detail"]
    assert _upload_dir_files() == uploads


def test_upload_removes_file_when_text_too_long(
    client: TestClient,
    auth_headers: dict[str, str],
    uploads: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL 的 TEXT 只有 65535 字节，中文占 3 字节，所以按 60000 字节卡。

    不在这里拦住的话，INSERT 时会抛 "Data too long"，用户看到的是 500，
    而且这时候文件已经落盘了。
    """
    monkeypatch.setattr(
        "app.api.routes.resumes.extract_text",
        lambda path, suffix: "字" * 25_000,  # 75000 字节
    )

    response = client.post(
        "/resumes",
        headers=auth_headers,
        files={"file": ("简历.docx", b"PK\x03\x04" + b"0" * 64, DOCX_MIME)},
    )

    assert response.status_code == 400
    assert "正文过长" in response.json()["detail"]
    assert _upload_dir_files() == uploads


# ---------------------------------------------------------------- 列表 / 详情 / 删除


def _add_resume(db: Session, user: User, name: str = "r.pdf", raw_text: str = "正文") -> Resume:
    stored = f"{name}-{user.id}-{len(name)}.pdf"
    resume = Resume(
        user_id=user.id,
        original_filename=name,
        stored_filename=stored,
        file_size=100,
        char_count=len(raw_text),
        raw_text=raw_text,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


def test_list_returns_only_own_resumes(
    client: TestClient, db: Session, auth_headers: dict[str, str], user: User
) -> None:
    """两个查询都带 user_id，少了它任何人登录后都能翻别人的简历列表。"""
    _add_resume(db, user, "mine.pdf")
    other = User(email="other@example.com", hashed_password=hash_password("other-pass-1234"))
    db.add(other)
    db.commit()
    db.refresh(other)
    _add_resume(db, other, "theirs.pdf")

    response = client.get("/resumes", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["original_filename"] for item in body["items"]] == ["mine.pdf"]


def test_list_paginates(
    client: TestClient, db: Session, auth_headers: dict[str, str], user: User
) -> None:
    for index in range(3):
        _add_resume(db, user, f"r{index}.pdf")

    first = client.get("/resumes", params={"page": 1, "size": 2}, headers=auth_headers).json()
    second = client.get("/resumes", params={"page": 2, "size": 2}, headers=auth_headers).json()

    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    # 两页的 id 不重叠，说明 offset 算对了
    assert {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]} == set()


def test_get_other_users_resume_returns_404(
    client: TestClient, db: Session, auth_headers: dict[str, str]
) -> None:
    """404 而不是 403。403 等于承认「这个 id 存在，只是你没权限」，
    那本身就是一个可以用来枚举资源的信息。"""
    other = User(email="other@example.com", hashed_password=hash_password("other-pass-1234"))
    db.add(other)
    db.commit()
    db.refresh(other)
    theirs = _add_resume(db, other, "theirs.pdf")

    response = client.get(f"/resumes/{theirs.id}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "简历不存在"


def test_detail_truncates_raw_text(
    client: TestClient, db: Session, auth_headers: dict[str, str], user: User
) -> None:
    """详情只给前 500 字，截断发生在序列化层（field_serializer）。"""
    resume = _add_resume(db, user, "long.pdf", raw_text="字" * 600)

    body = client.get(f"/resumes/{resume.id}", headers=auth_headers).json()

    assert body["raw_text"].endswith("…")
    assert len(body["raw_text"]) == 501
    assert body["char_count"] == 600  # 库里存的还是全文，截断只影响出参


def test_delete_removes_file_and_cascades_analyses(
    client: TestClient, db: Session, auth_headers: dict[str, str], user: User
) -> None:
    stored = "delete-me.pdf"
    (settings.UPLOAD_DIR / stored).write_bytes(b"%PDF-1.4")
    resume = Resume(
        user_id=user.id,
        original_filename="delete-me.pdf",
        stored_filename=stored,
        file_size=8,
        char_count=3,
        raw_text="abc",
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    analysis = Analysis(resume_id=resume.id, user_id=user.id)
    db.add(analysis)
    db.commit()
    analysis_id = analysis.id

    response = client.delete(f"/resumes/{resume.id}", headers=auth_headers)

    assert response.status_code == 204
    assert not (settings.UPLOAD_DIR / stored).exists()
    assert db.scalar(select(Analysis).where(Analysis.id == analysis_id)) is None
