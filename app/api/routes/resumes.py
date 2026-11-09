"""简历上传、列表、详情、删除。

上传是整个项目里最容易出安全问题的地方：用户能直接控制文件名、
文件内容和文件大小。这里的每一步校验都不是多余的，删掉任何一步都能构造出攻击场景。
"""

from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.openapi import (
    AUTH_REQUIRED,
    BAD_REQUEST_UPLOAD,
    OWNED_RESUME,
    SAVE_FAILED,
    TOO_LARGE,
    UNSUPPORTED_MEDIA,
)
from app.core.config import settings
from app.db.session import get_db
from app.models.resume import Resume
from app.models.user import User
from app.schemas.resume import ResumeDetailOut, ResumeListOut, ResumeOut
from app.services.parser import extract_text

router = APIRouter(prefix="/resumes", tags=["简历"])

# 扩展名 -> 文件头魔数。两边必须同时匹配才放行：
# 扩展名是用户随便写的，文件内容才是真相。
_ALLOWED = {
    ".pdf": b"%PDF-",
    ".docx": b"PK\x03\x04",  # docx 本质是 zip 包，这是 zip 的本地文件头签名
}

_CHUNK_SIZE = 64 * 1024

# MySQL 的 TEXT 最多 65535 字节，中文按 utf8mb4 占 3 字节，
# 留出余量后按 60000 字节卡，避免出现 "Data too long for column" 的 500
_MAX_RAW_TEXT_BYTES = 60_000


def _remove_file(stored_filename: str) -> None:
    """删磁盘文件，不存在也不报错。

    统一走这个函数，是为了保证"删文件"这件事永远用 missing_ok=True，
    不会因为文件早就没了而把正常流程打断。
    """
    (settings.UPLOAD_DIR / stored_filename).unlink(missing_ok=True)


@router.post(
    "",
    response_model=ResumeOut,
    status_code=status.HTTP_201_CREATED,
    summary="上传简历并解析",
    description=(
        "支持 **PDF** 和 **DOCX**，默认上限 5MB（`MAX_UPLOAD_MB`）。"
        "上传后立刻解析出正文存库，所以这个接口是同步的、可能需要一两秒。\n\n"
        "校验顺序如下，任何一步失败都会把已落盘的文件删掉：\n\n"
        "1. **扩展名**（415）—— 纯用户输入，不可信\n"
        "2. **大小**（413）—— 边读边累加，超限立刻中断，不把整个文件读进内存\n"
        "3. **文件头魔数**（415）—— 有人把 .exe 改名成 .pdf，扩展名检查完全拦不住\n"
        "4. **解析**（400）—— 扫描件、损坏文件、加密 PDF 都在这里失败\n"
        "5. **正文长度**（400）—— 超过 MySQL TEXT 的余量上限就拒绝\n\n"
        "落盘时用 UUID 重命名，不保留原始文件名：原名里可能有 `../` 造成路径穿越，"
        "也可能和别人的文件同名直接覆盖。原文件名只存在数据库里用于展示。"
    ),
    responses={
        **AUTH_REQUIRED,
        400: BAD_REQUEST_UPLOAD,
        413: TOO_LARGE,
        415: UNSUPPORTED_MEDIA,
        500: SAVE_FAILED,
    },
)
def upload_resume(
    file: UploadFile = File(..., description="简历文件，.pdf 或 .docx"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Resume:
    # ---- 第 1 步：扩展名 ----
    original_name = (file.filename or "unnamed").strip()[:255]
    suffix = Path(original_name).suffix.lower()

    if suffix not in _ALLOWED:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"只支持 PDF 和 DOCX，收到的是 {suffix or '无扩展名的文件'}",
        )

    # ---- 第 2 步：分块读，边读边数大小 ----
    # 不用 await file.read() 一把梭：UploadFile 背后是临时文件，
    # 不带参数会把整个文件读进内存，一个几个 G 的文件直接能把进程打挂，
    # MAX_UPLOAD_MB 这个配置也就形同虚设了。
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    buffer = BytesIO()
    total = 0

    while chunk := file.file.read(_CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {settings.MAX_UPLOAD_MB}MB 上限",
            )
        buffer.write(chunk)

    data = buffer.getvalue()

    if total == 0:
        raise HTTPException(status_code=400, detail="文件是空的")

    # ---- 第 3 步：魔数校验 ----
    # 有人把 .exe 改名成 .pdf 传上来，扩展名检查完全拦不住。
    # 读文件头几个字节比对，才能确认它真是那类文件。
    if not data.startswith(_ALLOWED[suffix]):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="文件内容与扩展名不符，可能被改过后缀",
        )

    # ---- 第 4 步：用 UUID 落盘 ----
    # 绝不能用用户给的文件名：里面可能有 ../ 造成路径穿越，
    # 也可能和已有文件同名直接覆盖别人的简历。
    stored_filename = f"{uuid4().hex}{suffix}"
    dest = settings.UPLOAD_DIR / stored_filename
    dest.write_bytes(data)

    # 从这里开始，磁盘上已经有文件了。后面任何一步失败都必须把它删掉，
    # 否则 uploads/ 会慢慢堆满没有数据库记录的孤儿文件。
    try:
        text = extract_text(dest, suffix)
    except ValueError as exc:
        _remove_file(stored_filename)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if len(text.encode("utf-8")) > _MAX_RAW_TEXT_BYTES:
        _remove_file(stored_filename)
        raise HTTPException(status_code=400, detail="简历正文过长，请精简后再上传")

    resume = Resume(
        user_id=current_user.id,
        original_filename=original_name,
        stored_filename=stored_filename,
        file_size=total,
        char_count=len(text),
        raw_text=text,
    )
    db.add(resume)

    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        _remove_file(stored_filename)
        raise HTTPException(status_code=500, detail="保存简历失败") from exc

    # id 和 created_at 都由数据库生成，MySQL 没有 RETURNING，只能再查一次
    db.refresh(resume)
    return resume


@router.get(
    "",
    response_model=ResumeListOut,
    summary="我的简历列表",
    description=(
        "按创建时间**倒序**分页返回当前用户名的简历。\n\n"
        "**不包含正文**：一份简历几千字，20 条就是几百 KB 的 JSON，"
        "列表页只想回答「哪份、多大、什么时候传的」。需要正文调详情接口。\n\n"
        "排序键是 `(created_at DESC, id DESC)` —— `created_at` 只精确到秒，"
        "同一秒内连传两份时单靠它排序不稳定，分页会漏行或重复行。"
    ),
    responses=AUTH_REQUIRED,
)
def list_resumes(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    size: int = Query(20, ge=1, le=100, description="每页条数，最大 100"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResumeListOut:
    # 两个查询都带 user_id 条件，这是数据隔离的根本。
    # 少了它，任何人登录后都能翻别人的简历列表。
    total = db.scalar(
        select(func.count())
        .select_from(Resume)
        .where(Resume.user_id == current_user.id)
    ) or 0

    # created_at 只精确到秒，同一秒内传多份时排序不稳定，
    # 再加 id 作为次级排序键，保证分页不会漏行或重复行
    items = db.scalars(
        select(Resume)
        .where(Resume.user_id == current_user.id)
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    # 显式做一次 ORM -> schema 的转换，而不是把 ORM 对象直接塞进 items。
    # 运行时两种写法都能跑（response_model 反正会再校验一遍），
    # 但 items 声明的是 list[ResumeOut]，塞 Resume 进去是类型上的谎话，
    # 而且一旦以后给 ResumeOut 加了计算属性，自动转换会悄悄绕开它
    return ResumeListOut(
        total=total,
        page=page,
        size=size,
        items=[ResumeOut.model_validate(r) for r in items],
    )


@router.get(
    "/{resume_id}",
    response_model=ResumeDetailOut,
    summary="简历详情",
    description=(
        "比列表多返回 `raw_text`，但**只截前 500 字**。\n\n"
        "截断发生在序列化层而不是查询层：字段声明成 `str` 却只装一部分内容，"
        "读者容易误以为这就是全文；用序列化器表达「出参只给预览」语义更准确，"
        "也不会影响对象本身（详情逻辑将来要拿全文时不用再查一次库）。"
    ),
    responses=OWNED_RESUME,
)
def get_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Resume:
    # 用 db.get(Resume, resume_id) 是错的：它只按主键查，
    # A 用户把 URL 里的数字改成 B 用户的简历 id 就能读到别人的数据。
    # 条件里必须同时带 user_id。
    resume = db.scalar(
        select(Resume).where(
            Resume.id == resume_id,
            Resume.user_id == current_user.id,
        )
    )
    if resume is None:
        # 返回 404 而不是 403：404 等于"我不知道有这么个东西"，
        # 403 反而等于承认"这个 id 确实存在，只是你没权限"，
        # 后者等于给攻击者一个枚举 id 的信号
        raise HTTPException(status_code=404, detail="简历不存在")
    return resume


@router.delete(
    "/{resume_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除简历",
    description=(
        "同时删除数据库记录和磁盘文件，成功返回 **204**（无响应体）。\n\n"
        "删除顺序是**先删库、再删文件**。反过来如果文件删了但 commit 失败，"
        "库里会留一条指向不存在文件的记录，点开详情就 500；"
        "而现在的顺序出错顶多留一个孤儿文件，不影响数据一致性。\n\n"
        "简历下的分析记录由数据库外键 `ON DELETE CASCADE` 一起清掉。"
    ),
    responses=OWNED_RESUME,
)
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    resume = db.scalar(
        select(Resume).where(
            Resume.id == resume_id,
            Resume.user_id == current_user.id,
        )
    )
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")

    stored_filename = resume.stored_filename

    # 先删库再删文件。顺序反过来的话，删文件成功但 commit 失败，
    # 数据库里就会留一条指向不存在文件的记录，点开详情就 500。
    # 反过来出错顶多是磁盘留个孤儿文件，不影响数据一致性。
    db.delete(resume)
    db.commit()

    _remove_file(stored_filename)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
