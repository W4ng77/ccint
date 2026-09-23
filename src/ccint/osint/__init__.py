"""开源情报关联:把帖子里提到的东西接到系统之外的权威事实上。"""
from .cve import CVE_RE, extract, enrich, link_corpus  # noqa: F401
