"""系统级接口的出入参。

只有一个模型，看起来不值得单独建文件 —— 但把它塞进 resume.py 或 user.py
都说不通：健康检查跟简历、跟用户都没有关系。按"谁的接口"归位，
比按"文件数量"归位重要。
"""

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    """健康检查响应。

    字段刻意保持极小：探活接口可能被负载均衡每秒调用一次，响应体越大越浪费。

    **故意不检查 MySQL / Redis**：一旦把依赖状态塞进来，数据库抖一下，
    负载均衡就会把整个服务摘掉 —— 本来只是"分析暂时不可用"，
    结果连登录、查列表都跟着 502，故障面被放大。
    依赖探活交给运维侧的监控，不放进这个接口。
    """

    status: str = Field(description="固定为 `ok`")
    version: str = Field(description="服务版本号，来自 `APP_VERSION`")
    debug: bool = Field(description="是否调试模式。生产环境应为 `false`")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"status": "ok", "version": "0.1.0", "debug": False}
        }
    )
