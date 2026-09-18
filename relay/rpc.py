from __future__ import annotations

from typing import Any


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


async def rpc(app: Any, method: str, **kwargs: Any) -> Any:
    return await getattr(app, rpcname(method))(**kwargs)
