"""MinerU.net 云端(v4) 适配器：把一个公开 PDF URL 解析为 content_list。

与 mineru_client.py(自建 http / 本地 cli) 区分开：云端是异步流程——提交 URL
任务→轮询至终态→下载结果 ZIP→读取其中的 content_list.json——故独立成模块，
入口接收 URL 而非本地文件。网络 I/O 经 `_http_json` / `_http_bytes` / `_sleep`
三个可覆写的接缝完成，编排逻辑可在不打网络的情况下单测。Bearer token 不入日志。
"""
from __future__ import annotations

import io
import json
import math
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional

from app.core.config import Settings
from app.services.mineru_http_retry import call_with_mineru_http_retries


class MinerUCloudNotConfigured(RuntimeError):
    """URL 来源被请求但未配置 MINERU_API_TOKEN 时抛出。"""


class MinerUCloudClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return self.settings.mineru_cloud_enabled

    def parse_url(self, url: str, *, data_id: str = "") -> List[dict]:
        """提交 PDF URL 给云端并返回 content_list(向后兼容，委托 parse_url_with_images)。

        未配置 token → MinerUCloudNotConfigured；云端失败/超时/结果不可用 → RuntimeError。
        """
        return self.parse_url_with_images(url, data_id=data_id)[0]

    def parse_url_with_images(
        self, url: str, *, data_id: str = ""
    ) -> tuple[List[dict], dict[str, bytes]]:
        """提交 PDF URL 给云端，返回 (content_list, {basename: 图片字节})。

        图片从同一份结果 ZIP 中抽取(仅当 settings.mineru_return_images 开启)，
        不产生额外网络请求。未配置 token → MinerUCloudNotConfigured；
        云端失败/超时/结果不可用 → RuntimeError。
        """
        self.last_error = ""
        if not self.configured:
            raise MinerUCloudNotConfigured("未配置 MinerU 云端凭证 (MINERU_API_TOKEN)")
        try:
            task_id = self._submit(url, data_id)
            zip_url = self._poll(task_id)
            zip_bytes = self._request_bytes(zip_url)
            content_list = self._content_list_from_zip(zip_bytes)
            if not content_list:
                raise RuntimeError("MinerU 云端结果为空 content_list")
            images = _images_from_zip(zip_bytes) if self.settings.mineru_return_images else {}
            return content_list, images
        except Exception as exc:
            if not self.last_error:
                self.last_error = str(exc)
            raise

    def parse_file_with_images(
        self, path: str, *, data_id: str = ""
    ) -> tuple[List[dict], dict[str, bytes]]:
        """上传本地文件给云端(v4 file-urls/batch)，返回 (content_list, {basename: 图片字节})。

        与 parse_url_with_images 对称，但走"申请上传URL→PUT上传→轮询batch结果→下载ZIP"。
        未配置 token → MinerUCloudNotConfigured；失败/超时/结果不可用 → RuntimeError。
        """
        self.last_error = ""
        if not self.configured:
            raise MinerUCloudNotConfigured("未配置 MinerU 云端凭证 (MINERU_API_TOKEN)")
        try:
            file_path = Path(path)
            batch_id, item_data_id = self._submit_file(file_path, data_id)
            zip_url = self._poll_batch(batch_id, item_data_id)
            zip_bytes = self._request_bytes(zip_url)
            content_list = self._content_list_from_zip(zip_bytes)
            if not content_list:
                raise RuntimeError("MinerU 云端结果为空 content_list")
            images = _images_from_zip(zip_bytes) if self.settings.mineru_return_images else {}
            return content_list, images
        except Exception as exc:
            if not self.last_error:
                self.last_error = str(exc)
            raise

    # -- steps -----------------------------------------------------------------

    def _submit(self, url: str, data_id: str) -> str:
        body = {
            "url": url,
            "model_version": self.settings.mineru_cloud_model_version,
            "is_ocr": False,
            "enable_formula": self.settings.mineru_cloud_formula_enable,
            "enable_table": self.settings.mineru_cloud_table_enable,
            "language": self.settings.mineru_cloud_language,
        }
        if data_id:
            body["data_id"] = data_id
        payload = self._request_json(
            "POST", self._api("/api/v4/extract/task"), body
        )
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(f"MinerU 云端提交失败: {payload.get('msg') or payload}")
        task_id = (payload.get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError("MinerU 云端未返回 task_id")
        return str(task_id)

    def _submit_file(self, path: Path, data_id: str) -> tuple[str, str]:
        """申请上传 URL 并 PUT 上传文件，返回 (batch_id, 本文件 data_id)。"""
        item_data_id = data_id or path.stem
        body = {
            "files": [{"name": path.name, "data_id": item_data_id, "is_ocr": False}],
            "model_version": self.settings.mineru_cloud_model_version,
            "enable_formula": self.settings.mineru_cloud_formula_enable,
            "enable_table": self.settings.mineru_cloud_table_enable,
            "language": self.settings.mineru_cloud_language,
        }
        payload = self._request_json(
            "POST", self._api("/api/v4/file-urls/batch"), body
        )
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(f"MinerU 云端申请上传失败: {payload.get('msg') or payload}")
        data = payload.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise RuntimeError("MinerU 云端未返回 batch_id/file_urls")
        self._request_put_file(str(file_urls[0]), path.read_bytes())
        return str(batch_id), item_data_id

    def _poll(self, task_id: str) -> str:
        interval = max(1, int(self.settings.mineru_cloud_poll_interval_seconds))
        timeout = max(interval, int(self.settings.mineru_cloud_timeout_seconds))
        max_polls = max(1, math.ceil(timeout / interval))
        url = self._api(f"/api/v4/extract/task/{task_id}")
        for _ in range(max_polls):
            payload = self._request_json("GET", url)
            data = payload.get("data") or {}
            state = str(data.get("state", "")).lower()
            if state == "done":
                zip_url = data.get("full_zip_url")
                if not zip_url:
                    raise RuntimeError("MinerU 云端完成但缺少 full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise RuntimeError(f"MinerU 云端解析失败: {data.get('err_msg') or '未知错误'}")
            self._sleep(interval)
        raise RuntimeError(f"MinerU 云端轮询超时 (>{timeout}s)")

    def _poll_batch(self, batch_id: str, data_id: str) -> str:
        """轮询 batch 结果，返回本文件 done 时的 full_zip_url。"""
        interval = max(1, int(self.settings.mineru_cloud_poll_interval_seconds))
        timeout = max(interval, int(self.settings.mineru_cloud_timeout_seconds))
        max_polls = max(1, math.ceil(timeout / interval))
        url = self._api(f"/api/v4/extract-results/batch/{batch_id}")
        for _ in range(max_polls):
            payload = self._request_json("GET", url)
            data = payload.get("data") or {}
            results = data.get("extract_result") or []
            item = next((r for r in results if str(r.get("data_id")) == str(data_id)), None)
            state = str((item or {}).get("state", "")).lower()
            if state == "done":
                zip_url = (item or {}).get("full_zip_url")
                if not zip_url:
                    raise RuntimeError("MinerU 云端完成但缺少 full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise RuntimeError(
                    f"MinerU 云端解析失败: {(item or {}).get('err_msg') or '未知错误'}"
                )
            self._sleep(interval)
        raise RuntimeError(f"MinerU 云端轮询超时 (>{timeout}s)")

    def _content_list_from_zip(self, zip_bytes: bytes) -> List[dict]:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            for name in sorted(names):
                if name.endswith("_content_list.json"):
                    data = json.loads(archive.read(name).decode("utf-8"))
                    if isinstance(data, list):
                        return data
            # 回退：从合并 markdown 合成最小 content_list（段落/标题，page 0）。
            for name in sorted(names):
                if name.endswith(".md") and not name.endswith(("_content_list.md", "_model.md")):
                    text = archive.read(name).decode("utf-8", "replace")
                    return _content_list_from_markdown(text)
        return []

    # -- network seams (tests override these) ----------------------------------

    def _request_json(
        self, method: str, url: str, payload: Optional[dict] = None
    ) -> dict:
        return call_with_mineru_http_retries(
            lambda: self._http_json(method, url, payload),
            max_retries=self.settings.mineru_max_retries,
            sleep=self._sleep,
        )

    def _request_bytes(self, url: str) -> bytes:
        def read_nonempty() -> bytes:
            data = self._http_bytes(url)
            if not data:
                # A 2xx with an empty body is the same transient failure shape
                # seen from the JSON endpoint. ConnectionError deliberately
                # routes it through the shared retry classifier.
                raise ConnectionError("MinerU remote response body is empty")
            return data

        return call_with_mineru_http_retries(
            read_nonempty,
            max_retries=self.settings.mineru_max_retries,
            sleep=self._sleep,
        )

    def _request_put_file(self, url: str, data: bytes) -> None:
        call_with_mineru_http_retries(
            lambda: self._http_put_file(url, data),
            max_retries=self.settings.mineru_max_retries,
            sleep=self._sleep,
        )

    def _http_json(self, method: str, url: str, payload: Optional[dict] = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.settings.mineru_api_token}")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def _http_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(
            request, timeout=self.settings.mineru_cloud_timeout_seconds
        ) as response:
            return response.read()

    def _http_put_file(self, url: str, data: bytes) -> None:
        """PUT 上传文件字节到签名 URL(不带 Content-Type，按 MinerU 官方示例)。"""
        request = urllib.request.Request(url, data=data, method="PUT")
        request.add_header("Content-Type", "")
        with urllib.request.urlopen(
            request, timeout=self.settings.mineru_cloud_timeout_seconds
        ) as response:
            status = getattr(response, "status", 200) or 200
            if status not in (200, 201, 204):
                raise RuntimeError(f"MinerU 云端上传失败 HTTP {status}")

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _api(self, path: str) -> str:
        return self.settings.mineru_api_base.rstrip("/") + path


def _images_from_zip(zip_bytes: bytes) -> dict[str, bytes]:
    """从结果 ZIP 中抽取 `images/` 目录下的图片，按 basename 为键。

    与 `_content_list_from_zip` 复用同一份 zip_bytes，不产生额外网络请求；
    跳过目录条目，仅匹配路径中含 `images` 目录分量的文件(含子目录)。
    单张图片条目损坏(BadZipFile/CRC 校验失败等)会被跳过而非抛出——
    与 HTTP 路径的 `_extract_images` 保持一致的“绝不因图片异常拖垮正文”语义。
    """
    images: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/")
            if "images" in parts[:-1]:
                try:
                    images[Path(name).name] = archive.read(name)
                except Exception:
                    continue   # 单张图损坏不影响其余图与已抽取的正文
    return images


def _content_list_from_markdown(text: str) -> List[dict]:
    blocks: List[dict] = []
    for chunk in text.split("\n\n"):
        body = chunk.strip()
        if not body:
            continue
        if body.startswith("#"):
            blocks.append({"type": "text", "text": body.lstrip("#").strip(),
                           "text_level": 1, "page_idx": 0})
        else:
            blocks.append({"type": "text", "text": body, "page_idx": 0})
    return blocks
