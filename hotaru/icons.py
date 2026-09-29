from __future__ import annotations

import html
from typing import Optional

# name: (id, fallback, emoji it is bound to); rich messages reject any other text in <tg-emoji>
_icons: dict[str, tuple[int, str, str]] = {
    "back": (5258236805890710909, "‹", "⬅️"),
    "close": (5260342697075416641, "✕", "❌"),
    "search": (5429571366384842791, "🔍", "🔎"),
    "settings": (5258096772776991776, "⚙️", "⚙"),
    "edit": (5258331647358540449, "✏️", "✍️"),
    "copy": (5258477770735885832, "📋", "📄"),
    "trash": (5258130763148172425, "🗑", "🗑"),
    "undo": (5260687119092817530, "↺", "🔄"),
    "redo": (5258420634785947640, "↻", "🔄"),
    "check": (5260726538302660868, "✓", "✅"),
    "done": (5260416304224936047, "✅", "✅"),
    "login": (5258215850745275216, "🔑", "➡️"),
    "logout": (5258084656674250503, "🚪", "🚪"),
    "ban": (5258318620722733379, "⛔", "❌"),
    "lock": (5258476306152038031, "🔒", "🔒"),
    "loading": (5258012149036365477, "◌", "📸"),
    "minus": (5275969776668134187, "−", "⛔️"),
    "book": (5258328383183396223, "📖", "📖"),
    "bolt": (5258152182150077732, "⚡", "⚡️"),
    "users": (5258513401784573443, "👥", "👥"),
    "user_add": (5258362837411045098, "👤", "👤"),
    "account": (5260399854500191689, "👤", "👤"),
    "phone": (5258337316715373336, "📞", "🤙"),
    "camera": (5258205968025525531, "📷", "📸"),
    "file": (5257965810634202885, "📄", "📁"),
    "download": (5258514780469075716, "📥", "📂"),
    "info": (5258503720928288433, "ℹ️", "ℹ️"),
    "warning": (5258474669769497337, "⚠️", "❗️"),
    "clock": (5258419835922030550, "🕒", "🕔"),
    "translate": (5260512129240276089, "🌐", "📚"),
    "idea": (5258216851472654189, "💡", "💡"),
    "text": (5370546867786523009, "🔤", "📝"),
    "kernel": (4974681956907221809, "▪️", "▪️"),
    "module": (4974508259839836856, "▪️", "▪️"),
    "hotaru": (5884064191467233197, "◉", "⚡️"),
    "hotaru_ho": (5886680238867357294, "HO", "⚡️"),
    "hotaru_ta": (5884059780535821183, "TA", "⚡️"),
    "hotaru_ru": (5884240418270356231, "RU", "⚡️"),
}

_modules = {
    "help": "book",
    "config": "settings",
    "accounts": "users",
    "acl": "lock",
    "executor": "text",
    "updater": "download",
    "translations": "translate",
    "logger": "file",
    "info": "info",
    "ping": "bolt",
    "power": "logout",
    "settings": "edit",
    "catalyst": "download",
}


def get(name: Optional[str]) -> Optional[tuple[int, str, str]]:
    return _icons.get(name) if name else None


def emoji(name: Optional[str]) -> str:
    icon = get(name)
    return f'<tg-emoji emoji-id="{icon[0]}">{html.escape(icon[2], quote=False)}</tg-emoji>' if icon else ""


def fallback(name: Optional[str]) -> str:
    icon = get(name)
    return icon[1] if icon else ""


def wordmark() -> str:
    return "".join(emoji(name) for name in ("hotaru", "hotaru_ho", "hotaru_ta", "hotaru_ru"))


def module(module_id: str) -> str:
    return _modules.get(module_id, "module")
