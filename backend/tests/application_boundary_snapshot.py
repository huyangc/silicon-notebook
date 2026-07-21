from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute


def _segments(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip("/").split("/") if part)


def _dynamic(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _overlap(left: str, right: str) -> bool:
    left_parts, right_parts = _segments(left), _segments(right)
    if len(left_parts) != len(right_parts):
        return False
    return all(
        a == b or _dynamic(a) or _dynamic(b)
        for a, b in zip(left_parts, right_parts, strict=True)
    )


def _api_routes(app: FastAPI) -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _collision_order(app: FastAPI) -> list[dict[str, str]]:
    routes = _api_routes(app)
    collisions: list[dict[str, str]] = []
    for index, left in enumerate(routes):
        for right in routes[index + 1 :]:
            methods = sorted(
                (left.methods or set()) & (right.methods or set()) - {"HEAD", "OPTIONS"}
            )
            if not methods or not _overlap(left.path, right.path):
                continue
            for method in methods:
                collisions.append(
                    {
                        "method": method,
                        "first_path": left.path,
                        "first_name": left.name,
                        "second_path": right.path,
                        "second_name": right.name,
                    }
                )
    return collisions


def snapshot(app: FastAPI) -> dict[str, Any]:
    return {
        "openapi": app.openapi(),
        "collision_order": _collision_order(app),
    }


def write_snapshot(app: FastAPI, target: Path) -> None:
    target.write_text(
        json.dumps(snapshot(app), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
