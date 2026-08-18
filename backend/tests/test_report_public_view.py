"""T6（深度报告引用卡附图）公开分享白名单守卫：`report_public_view.
public_reference()` 是显式白名单投影（不是脱敏管线），本文件钉死 `images`/
`asset_id` 永不跨出匿名边界——这条红线的完整背景见模块自身的 docstring。

`public_reference()` 逐字构造一个只含 5 个键的新 dict，天然不会转发调用方塞进
输入 dict 的任何额外键；但「天然安全」不是「经过验证」，所以本文件仍显式钉住它
——即使未来有人把这份函数改写成"复制输入再删几个键"的形状，这条测试也应该
报红。见 T6 任务简报要求的删除变异验证（在本次实现的验证记录里，非本文件内容）。
"""
from __future__ import annotations

from app.services.report_public_view import public_reference


def test_public_reference_never_carries_images_even_when_the_input_has_them():
    reference = {
        "key": "k1",
        "source_title": "时序手册",
        "source_file_name": "timing.md",
        "location_label": "§1",
        "snippet": "被引用的原文片段",
        # 这些字段一旦跨出，就是把内部 handle 或图片资产 id 发给匿名访客。
        "images": [
            {"element_id": "el-secret-fig", "asset_id": "asset-secret-1",
             "caption": "机密图注"},
        ],
        "source_id": "src-secret",
        "element_id": "el-secret",
        "object_id": "ko-secret",
    }

    projected = public_reference(reference)

    assert "images" not in projected
    assert "asset_id" not in projected
    assert set(projected) == {"key", "title", "file_name", "location", "snippet"}
    serialized = str(projected)
    for secret in (
        "el-secret-fig", "asset-secret-1", "机密图注",
        "src-secret", "el-secret", "ko-secret",
    ):
        assert secret not in serialized


def test_public_reference_allowlist_shape_is_exactly_five_keys():
    """回归门：新增字段必须显式加进 `public_reference()` 才会出现在这里——
    这条断言本身就是"新增字段默认不过白名单"的存在性证明。"""
    projected = public_reference({"key": "k1", "label": "x"})
    assert set(projected) == {"key", "title", "file_name", "location", "snippet"}
