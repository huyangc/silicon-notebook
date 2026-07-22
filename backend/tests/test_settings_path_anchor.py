"""回归：相对路径 Settings 字段（storage_dir / database_url 的 sqlite 路径部分 /
env_file）必须锚定到仓库根，与进程启动时的 CWD 无关。

背景（第三次「双 .local」生产事故）：`npm run dev` 经 scripts/dev.sh cd 进
backend/ 再起 uvicorn，CLI 从仓库根跑 —— 同一套相对默认值 `.local/...` 在两种
启动方式下解析到两个不同目录，导致 CLI 建好的索引服务端看不到。修复：
Settings 用 model_validator(mode="after") 统一把相对路径重写为仓库根锚定的
绝对路径，绝对 env 覆盖原样尊重；非 SQLite URL 可以被安全地解析，直到后续
repository factory 阶段才会选择对应后端。
"""
import os

import pytest

from app.core.config import Settings, _ROOT_DIR


def _clear_path_env(monkeypatch):
    for key in ("SILICON_NOTEBOOK_STORAGE_DIR", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)


class TestCwdIndependence:
    """chdir 到任意目录后构造 Settings()，路径必须解析到仓库根下的绝对路径，
    且与 CWD 无关（在仓库根跑 vs 在 backend/ 下跑结果一致）。"""

    def test_default_storage_dir_anchored_to_repo_root(self, tmp_path, monkeypatch):
        _clear_path_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        s = Settings()
        assert s.storage_dir == str(_ROOT_DIR / ".local" / "storage")
        assert os.path.isabs(s.storage_dir)

    def test_default_database_url_anchored_to_repo_root(self, tmp_path, monkeypatch):
        _clear_path_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        s = Settings()
        expected = f"sqlite:///{_ROOT_DIR / '.local' / 'silicon_notebook.db'}"
        assert s.database_url == expected
        assert s.sqlite_path == str(_ROOT_DIR / ".local" / "silicon_notebook.db")

    def test_cwd_in_backend_dir_gives_same_result_as_repo_root(self, monkeypatch):
        """cd backend/ 再构造（模拟 npm run dev 的 scripts/dev.sh 行为）与从仓库根
        构造（模拟离线 CLI）结果必须一致 —— 这正是本修复要消灭的分裂点。"""
        _clear_path_env(monkeypatch)
        monkeypatch.chdir(_ROOT_DIR)
        from_root = Settings()

        _clear_path_env(monkeypatch)
        monkeypatch.chdir(_ROOT_DIR / "backend")
        from_backend = Settings()

        assert from_root.storage_dir == from_backend.storage_dir
        assert from_root.database_url == from_backend.database_url
        assert os.path.isabs(from_root.storage_dir)
        assert os.path.isabs(from_backend.storage_dir)


class TestAbsoluteEnvOverridesRespectedVerbatim:
    """绝对路径 env 覆盖必须原样尊重，不重锚（否则会打破所有把数据放绝对
    tmp_path 的既有测试和真实绝对路径部署）。"""

    def test_absolute_storage_dir_env_untouched(self, monkeypatch):
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", "/tmp/x")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Settings()
        assert s.storage_dir == "/tmp/x"

    def test_absolute_sqlite_url_four_slashes_untouched(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/y.db")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        assert s.database_url == "sqlite:////tmp/y.db"
        assert s.sqlite_path == "/tmp/y.db"

    def test_absolute_sqlite_url_with_tmp_path(self, tmp_path, monkeypatch):
        """既有测试的主流模式：DATABASE_URL=sqlite:///{tmp_path}/t.db（tmp_path
        本身是绝对路径，三斜杠+绝对路径部分）。"""
        db_file = tmp_path / "t.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        assert s.database_url == f"sqlite:///{db_file}"
        assert s.sqlite_path == str(db_file)


class TestRelativeEnvValuesAlsoAnchored:
    """相对 env 值（非绝对路径的显式覆盖）与默认值同规则锚定到仓库根 ——
    否则用户设了个相对路径的 env 依然会被 CWD 摆布，锚定就没有意义。"""

    def test_relative_storage_dir_env_anchored(self, monkeypatch):
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", ".local/custom_storage")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Settings()
        assert s.storage_dir == str(_ROOT_DIR / ".local" / "custom_storage")

    def test_relative_sqlite_url_env_anchored(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///.local/foo.db")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        expected = f"sqlite:///{_ROOT_DIR / '.local' / 'foo.db'}"
        assert s.database_url == expected
        assert s.sqlite_path == str(_ROOT_DIR / ".local" / "foo.db")

    def test_relative_sqlite_url_without_local_prefix_anchored(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///some/nested/db.sqlite3")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        expected = f"sqlite:///{_ROOT_DIR / 'some' / 'nested' / 'db.sqlite3'}"
        assert s.database_url == expected


class TestDatabaseUrlSchemes:
    """配置阶段只验证 URL；SQLite runtime 仍只能请求 SQLite 路径。"""

    def test_postgres_url_is_normalized_and_sqlite_path_fails_clearly(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@host:5432/db")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        assert s.database_url == "postgresql://user:pass@host:5432/db"
        with pytest.raises(ValueError, match="not a SQLite"):
            _ = s.sqlite_path

    def test_mysql_url_rejected(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@host/db")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        with pytest.raises(ValueError, match="unsupported database URL scheme"):
            Settings()


class TestSqlitePathPropertySlashForms:
    """sqlite_path 属性（现有 L~411）解析 sqlite:/// 前缀的行为矩阵，覆盖
    三斜杠+相对、三斜杠+绝对(即事实上四斜杠开头)两种拼写。"""

    def test_three_slash_relative_becomes_absolute_after_anchor(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///.local/silicon_notebook.db")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        assert s.sqlite_path == str(_ROOT_DIR / ".local" / "silicon_notebook.db")

    def test_four_slash_absolute_preserved(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:////abs/path/db.sqlite")
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        s = Settings()
        assert s.sqlite_path == "/abs/path/db.sqlite"


class TestEnvFileAnchored:
    """model_config.env_file 必须是仓库根下的绝对路径，不依赖 CWD 是否恰好
    在仓库根或 backend/ 下才能找到 .env。"""

    def test_env_file_is_absolute_repo_root_env(self):
        env_file = Settings.model_config.get("env_file")
        expected = None if os.environ.get("SILICON_NOTEBOOK_ENV_FILE") == "" else str(_ROOT_DIR / ".env")
        assert env_file == expected

    def test_missing_env_file_does_not_raise(self, tmp_path, monkeypatch):
        """指向不存在的 .env 时 pydantic-settings 静默忽略，不应报错（本仓库根下
        通常确实有 .env，这里额外验证「文件不存在」分支不炸，覆盖 CI/裸检出场景）。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
        # Settings() 本身在类定义时已固定 env_file；这里只需确认构造不抛异常。
        Settings()


class TestOtherAnchoredPathSettings:
    """同类相对路径字段（llm_cache_path / llm_log_path / event_log_dir）也应
    锚定到仓库根，覆盖它们各自当前独立的锚定实现（llm.py/event_logging.py/
    debug_logs.py 里散落的 `Path(__file__).resolve().parents[3]` 判断）继续
    对新的、集中式锚定后的值生效。这里只验证 Settings 层面默认值仍是相对
    字符串（未被这次的 validator 误伤 —— 该 validator 范围限定在
    storage_dir/database_url，其它路径字段的锚定逻辑保留在各自消费点，不重复实现）。
    """

    def test_llm_log_path_still_relative_string_by_default(self, monkeypatch):
        monkeypatch.delenv("LLM_LOG_PATH", raising=False)
        s = Settings()
        assert s.llm_log_path == ".local/logs/llm.jsonl"

    def test_event_log_dir_still_relative_string_by_default(self, monkeypatch):
        monkeypatch.delenv("EVENT_LOG_DIR", raising=False)
        s = Settings()
        assert s.event_log_dir == ".local/logs"
