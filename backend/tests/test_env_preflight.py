"""启动 .env 预检:后端只读仓库根 .env(Next.js 的「Environments: .env.local」
只代表前端自己)。缺 .env 且根目录存在改名残骸(如 .env.local)→ 硬报错;
单纯缺失 → 仅告警(全新 checkout / 容器纯环境变量部署仍可启动);
ALLOW_NO_ENV_FILE=1 显式跳过硬报错(降级必须 opt-in)。"""
import pytest

from app.core.config import env_file_diagnosis


def test_diagnosis_env_present(tmp_path):
    (tmp_path / ".env").write_text("A=1\n")
    (tmp_path / ".env.local").write_text("B=2\n")
    path, exists, lookalikes = env_file_diagnosis(tmp_path)
    assert path == tmp_path / ".env"
    assert exists is True
    # .env 在位时 lookalike 无所谓(比如用户留着备份),不报
    assert lookalikes == []


def test_diagnosis_missing_with_lookalike(tmp_path):
    (tmp_path / ".env.local").write_text("A=1\n")
    (tmp_path / ".env.bak").write_text("A=1\n")
    (tmp_path / ".env.example").write_text("A=1\n")  # 合法模板,不算残骸
    _, exists, lookalikes = env_file_diagnosis(tmp_path)
    assert exists is False
    assert lookalikes == [".env.bak", ".env.local"]


def test_diagnosis_missing_clean(tmp_path):
    (tmp_path / ".env.example").write_text("A=1\n")
    _, exists, lookalikes = env_file_diagnosis(tmp_path)
    assert exists is False
    assert lookalikes == []


def _patch_root(monkeypatch, tmp_path):
    """把预检的仓库根指到 tmp_path(diagnosis 运行时读 config._ROOT_DIR)。"""
    import app.core.config as config_mod
    monkeypatch.setattr(config_mod, "_ROOT_DIR", tmp_path)


def test_create_app_fails_on_lookalike(monkeypatch, tmp_path):
    # import 须在 patch 之前:app.main 模块级就有 `app = create_app()`(保证
    # `uvicorn app.main:app` 必触发预检),首次 import 用真实仓库根安全通过。
    from app.main import create_app
    (tmp_path / ".env.local").write_text("A=1\n")
    _patch_root(monkeypatch, tmp_path)
    monkeypatch.delenv("ALLOW_NO_ENV_FILE", raising=False)
    with pytest.raises(RuntimeError, match=r"\.env\.local"):
        create_app()


def test_create_app_lookalike_optout(monkeypatch, tmp_path):
    from app.main import create_app
    (tmp_path / ".env.local").write_text("A=1\n")
    _patch_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ALLOW_NO_ENV_FILE", "1")
    assert create_app() is not None


def test_create_app_plain_missing_warns_but_boots(monkeypatch, tmp_path, caplog):
    from app.main import create_app
    _patch_root(monkeypatch, tmp_path)
    monkeypatch.delenv("ALLOW_NO_ENV_FILE", raising=False)
    import logging
    with caplog.at_level(logging.WARNING, logger="silicon_notebook.startup"):
        app = create_app()
    assert app is not None
    assert any(".env" in r.message for r in caplog.records)
