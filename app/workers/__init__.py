"""后台任务包。

这里刻意留空，不在 __init__ 里 import tasks：
`celery -A app.workers.celery_app` 加载本包时 celery_app 还没构造完，
此时 import tasks 会反过来 import celery_app，形成循环导入。
任务模块由 Celery(include=[...]) 延迟加载。
"""
