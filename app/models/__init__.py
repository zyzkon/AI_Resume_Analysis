"""所有 ORM 模型的统一出口。

为什么要有这个文件：Base.metadata 只有在模型类被 import 之后才会登记表结构。
create_all 遍历的是这份登记簿，所以少 import 一个模块，那张表就不会被创建。
统一从这里导出，调用方只需 `from app.models import User, Resume, Analysis`，
不必关心每个类在哪个文件里，也不会漏。
"""

from app.models.analysis import Analysis, AnalysisStatus
from app.models.resume import Resume
from app.models.user import User

__all__ = ["Analysis", "AnalysisStatus", "Resume", "User"]
