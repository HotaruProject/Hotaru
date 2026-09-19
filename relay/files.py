from __future__ import annotations

from pathlib import Path
from typing import Any


def document(up: dict[str, Any], *, mime: str = "application/octet-stream", file_name: str | None = None, force_file: bool = True) -> dict[str, Any]:
    name = file_name or up.get("name") or "file"
    if up.get("big"):
        file = {"_": "inputFileBig", "id": up["id"], "parts": up["parts"], "name": up["name"]}
    else:
        file = {"_": "inputFile", "id": up["id"], "parts": up["parts"], "name": up["name"], "md5_checksum": up.get("md5", "")}
    return {
        "_": "inputMediaUploadedDocument",
        "file": file,
        "mime_type": mime,
        "attributes": [{"_": "documentAttributeFilename", "file_name": name}],
        "force_file": force_file,
    }


async def put(app: Any, source: Any, *, file_name: str | None = None, **kw: Any) -> dict[str, Any]:
    return await app.charged_upload(source, file_name=file_name, **kw)


async def take(app: Any, source: Any, destination: str | Path, **kw: Any) -> Any:
    return await app.download_media(source, str(destination), **kw)
