"""只读演示门户。

[MUST] 不在这里 import ``app``。``app`` 依赖 ``agent.assistant``,而后者依赖
``agent.toolset``,后者又要 import ``portal.queries`` —— 包级的急切导入会把这条
链闭合成循环。查询层必须能脱离 web 层单独导入,agent 和测试都只要它。
"""
