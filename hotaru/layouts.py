from __future__ import annotations

_EN_CHARS = "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~@#$^&"
_RU_CHARS = "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё\"№;:?"

_EN_TO_RU = {en: ru for en, ru in zip(_EN_CHARS, _RU_CHARS)}
_RU_TO_EN = {ru: en for en, ru in zip(_EN_CHARS, _RU_CHARS)}


def swap_layout(value: str) -> str:
    out: list[str] = []
    for char in value:
        if char in _EN_TO_RU:
            out.append(_EN_TO_RU[char])
        elif char in _RU_TO_EN:
            out.append(_RU_TO_EN[char])
        else:
            out.append(char)
    return "".join(out)
