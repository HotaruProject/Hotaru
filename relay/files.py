from __future__ import annotations

from pathlib import Path
from typing import Any

from relay.firewall import trusted_scope


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
    with trusted_scope():
        return await app.charged_upload(source, file_name=file_name, **kw)


async def take(app: Any, source: Any, destination: str | Path, **kw: Any) -> Any:
    dest = str(destination)
    with trusted_scope():
        obj: Any = source
        if isinstance(obj, tuple) and len(obj) == 2:
            obj = await app.get_msg(obj[0], obj[1])
        media = None
        if isinstance(obj, dict):
            media = obj.get("media") if isinstance(obj.get("media"), dict) else obj
        else:
            media = getattr(obj, "media", None)
            raw = getattr(obj, "raw", None)
            if media is None and isinstance(raw, dict):
                media = raw.get("media")
        location = app._media_location(media) or app._media_location(obj if isinstance(obj, dict) else getattr(obj, "raw", None))
        if location is None:
            raise ValueError("no downloadable media found in source")
        size = 0
        doc = media.get("document") if isinstance(media, dict) and isinstance(media.get("document"), dict) else media
        if isinstance(doc, dict):
            size = int(doc.get("size") or 0)
        await app.charged_download(location, dest, size=size, media_source=media, **kw)
        return dest
