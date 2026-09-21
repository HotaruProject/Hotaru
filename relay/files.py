from __future__ import annotations

from pathlib import Path
from typing import Any, cast

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
        if isinstance(obj, tuple):
            pair = cast('tuple[object, ...]', obj)
            if len(pair) == 2:
                obj = await app.get_msg(pair[0], pair[1])
            else:
                raise ValueError("message tuple must contain peer and id")
        media: Any = None
        if isinstance(obj, dict):
            raw_obj = cast('dict[str, Any]', obj)
            media = raw_obj.get("media") if isinstance(raw_obj.get("media"), dict) else raw_obj
        else:
            media = cast(Any, getattr(obj, "media", None))
            raw = cast(Any, getattr(obj, "raw", None))
            if media is None and isinstance(raw, dict):
                media = cast('dict[str, Any]', raw).get("media")
        location = app._media_location(media) or app._media_location(obj if isinstance(obj, dict) else cast(Any, getattr(obj, "raw", None)))
        if location is None:
            raise ValueError("no downloadable media found in source")
        size = 0
        media_dict = cast('dict[str, Any]', media) if isinstance(media, dict) else None
        document_value = media_dict.get("document") if media_dict is not None else None
        doc: Any = cast('dict[str, Any]', document_value) if isinstance(document_value, dict) else cast(Any, media)
        if isinstance(doc, dict):
            size = int(cast('dict[str, Any]', doc).get("size") or 0)
        await app.charged_download(location, dest, size=size, media_source=media, **kw)
        return dest
