from __future__ import annotations

from typing import Any, cast


def rpcname(method: str) -> str:
    method = str(method).strip()
    if method.startswith("mt_"):
        return method
    if "." not in method:
        return "mt_" + method
    ns, rest = method.split(".", 1)
    bits: list[str] = []
    for i, ch in enumerate(rest):
        if ch.isupper() and i:
            bits.append("_")
        bits.append(ch.lower())
    return "mt_" + ns + "_" + "".join(bits)


async def delete_chat_msg(app: Any, chat_id: Any, msg_id: int, revoke: bool = True) -> Any:
    ids = [int(msg_id)]
    if isinstance(chat_id, int) and chat_id <= -1000000000000:
        mt = getattr(app, "mt", None)
        if mt is None:
            raise RuntimeError("mt net is not configured")
        channel_id = -chat_id - 1000000000000
        await mt.resolve_peer(chat_id)
        entity = mt.entities.get(("chat", channel_id)) if getattr(mt, "entities", None) is not None else None
        access_hash = cast('dict[str, Any]', entity).get("access_hash", 0) if isinstance(entity, dict) else 0
        if not access_hash:
            raise ValueError("channel peer requires a non-zero access_hash")
        return await app.mt_channels_delete_messages(
            channel={"_": "inputChannel", "channel_id": channel_id, "access_hash": int(access_hash)},
            id=ids,
        )
    return await app.mt_messages_delete_messages(id=ids, revoke=revoke)
