"""路由层共用的依赖（Dependency）。

FastAPI 的 Depends 机制：函数签名里声明 Depends(x)，框架会在调用路由函数前
先执行 x，把结果作为参数传进来。好处是"认证"这类横切逻辑只写一遍，
所有需要登录的路由只要挂一个 Depends(get_current_user) 就自动被保护。

这个文件是 security.py（纯函数）和路由（HTTP 层）之间的翻译层：
把 JWT 字符串翻译成数据库里的 User 对象。
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import TokenError, decode_access_token
from app.db.session import get_db
from app.models.user import User

# 声明式配置，本身不做任何校验。
# 作用有三个：
#   1. 告诉 OpenAPI 文档"本项目用 OAuth2 密码模式"，Swagger UI 上才会出现 Authorize 按钮；
#   2. tokenUrl 指定前端该往哪个地址 POST 用户名密码去换 token，
#      这里的 "/auth/token" 必须和 auth 路由里实际注册的路径完全一致，否则点 Authorize 会 404；
#   3. auto_error=True（默认）时，请求头里没有 Authorization 就自动抛 401，
#      不用我们手写"没带 token"的判断。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """从 Authorization 请求头解析出当前用户，解析不出来就 401。

    注意这个函数不返回 None：要么返回 User，要么直接抛异常中断请求。
    所以路由里拿到的 current_user 一定非空，不需要写 if current_user is None。
    """

    # 统一的 401 响应。带 WWW-Authenticate: Bearer 是 RFC 6750 的硬性要求，
    # 缺了它 Swagger UI 和大多数 HTTP 客户端不会把它当成"认证失败"处理。
    # 故意不区分"token 过期"和"签名不对"，避免给攻击者试探的信息。
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的身份凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # 第一步：验签 + 检查过期。security.py 把 PyJWT 的一堆异常
    # 收敛成了 TokenError 一种，这里只捕获它。
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # 第二步：取出 sub（我们签发时塞的是用户 id 的字符串形式）。
    # 用 payload.get 而不是 payload["sub"]，因为 token 可能压根没这个字段
    # （比如手工构造的），缺字段应当是 401 而不是 KeyError 变 500。
    subject = payload.get("sub")
    if not subject:
        raise credentials_exception

    # 第三步：转回 int。sub 存的是字符串，直接拿去 db.get 会因为是 str 而查不到。
    # 也可能被人改成 "abc"，所以这里要兜住 ValueError。
    try:
        user_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise credentials_exception from exc

    # 第四步：查库。db.get 按主键查，命中不了返回 None。
    # 为什么每次请求都查库而不是只信 token 里的信息？
    # 因为 token 一旦签发就无法撤销——用户被禁用、账号被删，
    # 只要旧 token 没过期就还能用。查一次库（走主键索引，成本极低）能立刻生效。
    user = db.get(User, user_id)
    if user is None:
        raise credentials_exception

    # 被禁用的账号即使拿着有效 token 也拦住。
    # 403 而不是 401：身份是真实的，只是没有权限，两者语义不同。
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )

    return user
