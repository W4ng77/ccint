class CollectorError(Exception):
    """采集层可归因的错误；计入 collection_runs.n_error。"""


class RateLimited(CollectorError):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class AuthError(CollectorError):
    """鉴权失败。不重试——重试只会烧掉 10 次/天的 createSession 配额。"""
