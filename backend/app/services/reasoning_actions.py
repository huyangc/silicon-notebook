"""Legacy reflect 动作参数中跨调用方共享的稳定常量。"""

# 来源清单默认覆盖当前笔记本及勾选的参考库；显式 current_notebook 才收窄。
ENUMERATE_SCOPE_ALL = "all"
ENUMERATE_SCOPE_CURRENT_NOTEBOOK = "current_notebook"
ENUMERATE_SCOPES = (ENUMERATE_SCOPE_ALL, ENUMERATE_SCOPE_CURRENT_NOTEBOOK)

# 实际名称形状校验由 repositories.lexical_query.exact_probe_terms 拥有。
EXACT_TERM_SHAPE_NOTE = (
    "要像 set_db、config.yaml 这样带下划线或点;只用连字符连接的词还需带数字,如 GPT-4"
)
