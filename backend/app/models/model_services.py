from typing import Optional

from pydantic import BaseModel


class ModelServiceView(BaseModel):
    base_url: str = ""
    model: str = ""
    has_key: bool = False
    key_hint: str = ""          # 打码尾段，如 "…t123"；绝不含完整 key
    source: str = "system"      # user | system | none


class ModelServiceUpdate(BaseModel):
    # 三态：字段缺省=不变；""=清除；非空=设置。api_key 同理(缺省=保留原 key)。
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class ModelSettingsUpdate(BaseModel):
    llm: Optional[ModelServiceUpdate] = None
    reasoning_llm: Optional[ModelServiceUpdate] = None
    rewrite_llm: Optional[ModelServiceUpdate] = None
    kg_llm: Optional[ModelServiceUpdate] = None
    rerank: Optional[ModelServiceUpdate] = None


class ModelTestRequest(BaseModel):
    service: str
    base_url: str = ""
    api_key: Optional[str] = None   # 省略 → 用已存 key
    model: str = ""


class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: int = 0
    # 诊断字段:失败分支写 f"{type(exc).__name__}: {exc}",给日志和排查,前端不上屏。
    error: str = ""
    # 失败原因的稳定枚举。200 响应挂不上 X-User-Message 头,所以出处由 schema 承载;
    # 这里刻意存 **code 而不是中文文案**——文案归前端(vocabulary.ts 的
    # MODEL_TEST_ERROR),这样它才落在界面词汇守卫的作用域里。后端存中文会绕开那道
    # 守卫,「缺少 base_url / model / api_key」这种把字段名甩给用户的文案正是这么漏的。
    # 取值:unknown_service | missing_config | upstream_error(未知 code 前端走兜底)。
    code: str = ""
