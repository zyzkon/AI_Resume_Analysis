"""冒烟用例：确认测试装置本身是通的。

这一组不测业务，只验证四件事：应用能起、依赖覆盖生效、认证链路能走、
外键在 SQLite 上真的开着。装置坏了的话，后面所有用例的通过都没有意义。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.analysis import Analysis
from app.models.resume import Resume


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_protected_endpoint_requires_token(client: TestClient) -> None:
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "未提供身份凭证"
    # RFC 6750 要求 401 带这个头，缺了很多客户端不会按认证失败处理
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_token_resolves_to_user(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/auth/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["email"] == "tester@example.com"


def test_foreign_key_cascade_is_enabled(db: Session, resume: Resume) -> None:
    """绕过 ORM 直接删父行，只有数据库层的外键能清理子行。

    ORM 的 `cascade="all, delete-orphan"` 会在 `session.delete(resume)` 时
    自己把子记录删掉 —— 那样即使 PRAGMA 没开，这个用例也会通过。
    用原生 SQL 删，才能证明 SQLite 上的 ON DELETE CASCADE 真的生效了。
    """
    analysis = Analysis(resume_id=resume.id, user_id=resume.user_id)
    db.add(analysis)
    db.commit()
    analysis_id = analysis.id

    db.execute(text("DELETE FROM resumes WHERE id = :id"), {"id": resume.id})
    db.commit()

    remaining = db.scalar(select(Analysis).where(Analysis.id == analysis_id))
    assert remaining is None
