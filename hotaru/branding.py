from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, cast

from goygram import ext

from relay.emoji import to_rich
from relay.files import document, put
from relay.firewall import trusted_scope

_assets = Path(__file__).parent / "assets"
_banners = {
    "help": ("hotaru-help", "video", "help.mp4"),
    "brand": ("hotaru-brand", "photo", "logo.png"),
}


def banner(name: str = "brand") -> str:
    media_id, kind, _ = _banners[name]
    tag = "video" if kind == "video" else "img"
    return f'<{tag} src="tg://{kind}?id={media_id}"/>\n'


class Branding:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}

    async def rich(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"_": "inputRichMessageHTML", **to_rich(text)}
        needed = [(mid, kind, name) for mid, kind, name in _banners.values() if f"tg://{kind}?id={mid}" in text]
        if not needed:
            return payload
        app = self.runtime.inline.bot_app
        files: list[dict[str, Any]] = []
        async with self._lock:
            for media_id, kind, filename in needed:
                key = (id(app), media_id)
                cached = self._cache.get(key)
                if cached is not None and time.monotonic() - cached[0] < 1800:
                    files.append(cached[1])
                    continue
                try:
                    entry = await self._upload(app, media_id, kind, filename)
                except Exception as exc:
                    tag = "video" if kind == "video" else "img"
                    payload["html"] = payload["html"].replace(f'<{tag} src="tg://{kind}?id={media_id}"/>', "")
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("branding", "banner_failed", asset=media_id, error=type(exc).__name__)
                    continue
                self._cache[key] = (time.monotonic(), entry)
                files.append(entry)
        if files:
            payload["files"] = files
        return payload

    async def _upload(self, app: Any, media_id: str, kind: str, filename: str) -> dict[str, Any]:
        up = await put(app, str(_assets / filename), file_name=filename)
        upload = document(up, mime="video/mp4" if kind == "video" else "image/png", force_file=False)
        if kind == "photo":
            media = {"_": "inputMediaUploadedPhoto", "file": upload["file"]}
        else:
            media = upload
            media["attributes"].extend([
                {"_": "documentAttributeAnimated"},
                {"_": "documentAttributeVideo", "duration": 4.0, "w": 1920, "h": 1080, "supports_streaming": True},
            ])
        with trusted_scope():
            result = await app.mt_messages_upload_media(peer={"_": "inputPeerSelf"}, media=media)
        if not isinstance(result, dict):
            result = result.to_dict()
        result = cast(dict[str, Any], result)
        result = result.get("result", result)
        source: Any = cast(dict[str, Any], result)["photo" if kind == "photo" else "document"]
        if not isinstance(source, dict):
            source = source.to_dict()
        source = cast(dict[str, Any], source)
        ref: dict[str, Any] = {"_": "inputPhoto" if kind == "photo" else "inputDocument", **{k: source[k] for k in ("id", "access_hash", "file_reference")}}
        return {"_": "inputRichFilePhoto" if kind == "photo" else "inputRichFileDocument", "id": media_id, "photo" if kind == "photo" else "document": ref}

    async def apply_avatars(self, forum: Any, chat_id: int) -> None:
        state = self.runtime.state
        app = self.runtime.app
        path = _assets / "avatar.png"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        info = getattr(self.runtime.inline, "info", None)
        targets = [("forum", chat_id)]
        if info is not None:
            targets.append(("bot", info.bot_id))
        for kind, target in targets:
            key = f"brand-avatar-{kind}-{target}"
            if state.get_setting(key) == digest:
                continue
            try:
                if kind == "forum":
                    peer = forum._user_channel(chat_id)
                    if peer is None:
                        continue
                else:
                    assert info is not None
                    peer = await forum._user_bot(info.bot_id, info.username)
                    if peer is None:
                        continue
                up = await put(app, str(path), file_name=path.name)
                file = document(up)["file"]
                with trusted_scope():
                    if kind == "forum":
                        await app.mt_channels_edit_photo(channel=peer, photo={"_": "inputChatUploadedPhoto", "file": file})
                    else:
                        await app.mt_photos_upload_profile_photo(bot=peer, file=file)
                state.set_setting(key, digest)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("branding", "avatar_failed", target=kind, error=type(exc).__name__)

    async def apply_bot_avatar(self, info: Any) -> None:
        state = self.runtime.state
        app = self.runtime.app
        key = f"brand-avatar-bot-{info.bot_id}"
        try:
            path = _assets / "avatar.png"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if state.get_setting(key) == digest:
                return
            with trusted_scope():
                peer = await app.mt.resolve_peer("@" + info.username)
            if isinstance(peer, bytes):
                peer = ext.deserialize_constructor(peer)
            elif not isinstance(peer, dict):
                peer = peer.to_dict()
            peer = cast(dict[str, Any], peer)
            if peer.get("_") != "inputPeerUser" or peer.get("user_id") != info.bot_id:
                raise ValueError("resolved bot identity does not match")
            bot = {"_": "inputUser", "user_id": info.bot_id, "access_hash": peer["access_hash"]}
            up = await put(app, str(path), file_name=path.name)
            with trusted_scope():
                await app.mt_photos_upload_profile_photo(bot=bot, file=document(up)["file"])
            state.set_setting(key, digest)
        except Exception as exc:
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("branding", "avatar_failed", target="bot", error=type(exc).__name__)
