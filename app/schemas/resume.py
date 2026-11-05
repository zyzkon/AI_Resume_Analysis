"""简历相关的请求/响应模型。

这里和 models/resume.py 的分工：
models 决定"数据库里存什么"，schemas 决定"接口收发什么"。
两者刻意不重合 —— 比如 raw_text 存在库里，但列表接口绝不返回它。
"""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class ResumeOut(BaseModel):
    """简历摘要，用于上传响应和列表。

    注意没有 raw_text：一份简历正文几千字，列表返回 20 条就是几百 KB 的 JSON，
    既拖慢接口又浪费流量。需要正文时单独调详情接口。
    """

    id: int
    original_filename: str = Field(description="用户上传时的原始文件名")
    file_size: int = Field(description="字节数")
    char_count: int = Field(description="解析出的字符数")
    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 1,
                "original_filename": "张三-Java后端.pdf",
                "file_size": 284517,
                "char_count": 3821,
                "created_at": "2026-11-05T08:21:00Z",
            }
        },
    )

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        """MySQL 的 DATETIME 不保存时区，读出来是 naive 的，补上 UTC 标记。

        不补的话序列化成 "2026-11-05T08:21:00"（无时区），
        前端 new Date() 会按本地时间解析，显示结果差 8 小时。
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ResumeDetailOut(ResumeOut):
    """简历详情，额外带正文预览。"""

    raw_text: str = Field(description="解析出的正文，只返回前 500 字")

    @field_serializer("raw_text")
    def _truncate(self, value: str, _info) -> str:
        """序列化时才截断，校验阶段保留完整值。

        为什么不直接在路由里切 raw_text[:500]？
        因为那样字段声明成 str 却只装了一部分内容，读者容易误以为这是全文。
        用序列化器表达"出参只给预览"，语义更准确，也不会影响对象本身。
        """
        if len(value) <= 500:
            return value
        return value[:500] + "…"


class ResumeListOut(BaseModel):
    """分页列表。

    为什么不直接返回数组？因为分页必须有 total 才知道有没有下一页，
    裸数组塞不下这个信息。日期别改造成 {items: [...], total: n} 之外的结构，
    前端习惯了这个形状。
    """

    total: int = Field(description="符合条件的总条数")
    page: int
    size: int
    items: list[ResumeOut]
