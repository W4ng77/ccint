"""外部事件登记表接入。

这个包存在的唯一理由:给系统一个**不是自己产生的**参照物。

在它之前,ccint 的每一个 precision / recall 数字都可以追溯到同一个作者 ——
写规则的人、写 prompt 的人、做人工编码的人是同一个。κ=0.870 在这种条件下
量的是一致性,不是独立性。外部登记表由第三方发布、带时间戳、本系统无法影响,
它把「我们看见了多少」从一个自我评估变成一个有外部分母的测量。

[MUST] 登记表记录逐字存进 ``external_events.raw_payload``(P1)。登记方随时
可能改写或下架条目,而我们的结论建立在某个时点的登记内容上。
"""
from .ransomwarelive import canonical, fetch_country_victims, ingest  # noqa: F401
