"""Authenticated persistence of content that a live public capability exposes."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_user,
    mark_pulse,
    mirrored_notebook_error,
    notebook_access_repository,
    notebook_mirror_fence,
    repository,
    source_repository,
    user_error,
)
from app.api.source_routes import _document_capacity_limit, _validate_source_file, document_capacity_message
from app.core.config import get_settings
from app.domain.public_share_revision import public_share_revision
from app.models.identity import UserProfile
from app.repositories.ports import DocumentCapacityExceeded, UploadedSourceFile
from app.services.kg import scheduler as kg_scheduler
from app.services.public_share_import import build_saved_share

router = APIRouter()


class SavePublicShareRequest(BaseModel):
    notebook_id: str = Field(min_length=1)
    expected_revision: str = Field(min_length=1)
    title: str = ""
    turn_index: int | None = Field(default=None, ge=0)


class SavedPublicShare(BaseModel):
    source_id: str
    notebook_id: str
    reused: bool


def _save(kind: str, token: str, payload: SavePublicShareRequest, user: UserProfile, request: Request) -> SavedPublicShare:
    # The destination is strictly owned by the saving user, including for admins.
    try:
        owner_id, _role = source_repository().notebook_owner(payload.notebook_id)
    except KeyError:
        raise user_error(404, "笔记本无法访问，请选择自己的笔记本。") from None
    if owner_id != user.id or not notebook_access_repository().user_can_access_notebook(payload.notebook_id, user.id):
        raise user_error(404, "笔记本无法访问，请选择自己的笔记本。")
    mirror = notebook_mirror_fence("sources:write", payload.notebook_id)
    if mirror:
        raise mirrored_notebook_error(mirror)
    # Reuse exactly the public projection and its live creator/token checks.
    from app.api.ask_routes import public_conversation_asset_route, public_conversation_route
    from app.api.report_routes import public_report_route

    public = (public_conversation_route if kind == "conversations" else public_report_route)(token, Response()).model_dump()
    if payload.expected_revision != public_share_revision(public):
        raise user_error(409, "分享内容已有变化，请刷新分享页后重新保存。")
    title = payload.title.strip() or str(public.get("title") or public.get("question") or "分享回答")
    if not title:
        raise user_error(422, "请输入保存标题。")

    def read_image(alias: str) -> tuple[str, bytes]:
        response = public_conversation_asset_route(token, alias)
        path = Path(response.path)
        limit = get_settings().mineru_max_image_bytes
        if path.stat().st_size > limit:
            raise user_error(413, "分享图片超过当前保存上限，请联系管理员调整设置。")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise user_error(413, "分享图片超过当前保存上限，请联系管理员调整设置。")
        return response.media_type, content

    try:
        settings = get_settings()
        content = build_saved_share(public, kind=kind, title=title, turn_index=payload.turn_index, read_image=read_image,
                                    max_bytes=settings.source_upload_max_bytes,
                                    max_images=settings.mineru_max_images_per_source)
    except ValueError as exc:
        raise user_error(422, str(exc)) from None
    _validate_source_file("分享回答.md", len(content))
    try:
        results = source_repository().upload_sources(
            payload.notebook_id,
            [UploadedSourceFile(file_name="分享回答.md", content_type="text/markdown", content=content, title=title)],
            scheduler=lambda source_id: kg_scheduler.submit_job(repository().process_source, source_id),
            capacity_limit=_document_capacity_limit(payload.notebook_id),
        )
    except DocumentCapacityExceeded as exc:
        raise user_error(409, document_capacity_message(exc.current, exc.limit, 1)) from None
    except KeyError:
        raise HTTPException(404, "Notebook not found") from None
    result = results[0]
    mark_pulse(request, payload.notebook_id, "sources:write", user.id, user)
    return SavedPublicShare(source_id=result.id, notebook_id=payload.notebook_id, reused=result.reused)


@router.post("/public/conversations/{token}/save", response_model=SavedPublicShare)
def save_public_conversation(token: str, payload: SavePublicShareRequest, request: Request,
                             user: UserProfile = Depends(get_current_user)) -> SavedPublicShare:
    return _save("conversations", token, payload, user, request)


@router.post("/public/reports/{token}/save", response_model=SavedPublicShare)
def save_public_report(token: str, payload: SavePublicShareRequest, request: Request,
                       user: UserProfile = Depends(get_current_user)) -> SavedPublicShare:
    return _save("reports", token, payload, user, request)
