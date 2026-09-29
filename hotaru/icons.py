"""Kernel icons and Unicode fallbacks."""
from __future__ import annotations

import html
from typing import Dict, Optional, Tuple


# ID, fallback, bound emoji. Telegram requires the exact bound emoji in rich text.
_icons: Dict[str, Tuple[int, str, str]] = {
    # navigation and actions
    "back": (5258236805890710909, "‹", "⬅️"),
    "close": (5260342697075416641, "✕", "❌"),
    "search": (5429571366384842791, "🔍", "🔎"),
    "settings": (5258096772776991776, "⚙️", "⚙"),
    "edit": (5258331647358540449, "✏️", "✍️"),
    "copy": (5258477770735885832, "📋", "📄"),
    "trash": (5258130763148172425, "🗑", "🗑"),
    "undo": (5260687119092817530, "↺", "🔄"),
    "redo": (5258420634785947640, "↻", "🔄"),
    "add": (5258108352008823107, "➕", "➕"),
    "check": (5260726538302660868, "✓", "✅"),
    "done": (5260416304224936047, "✅", "✅"),
    "more": (5260348422266822411, "⋯", "💬"),
    "open": (5257991477358763590, "↗", "↗️"),
    "link": (5260730055880876557, "🔗", "⛓"),
    "save": (5258336354642697821, "💾", "⬇️"),
    "download": (5258514780469075716, "📥", "📂"),
    "upload": (5260652420052032852, "📤", "⬆️"),
    "share": (5258043150110301407, "📤", "⬆️"),
    "login": (5258215850745275216, "🔑", "➡️"),
    "logout": (5258084656674250503, "🚪", "🚪"),
    "ban": (5258318620722733379, "⛔", "❌"),
    "lock": (5258476306152038031, "🔒", "🔒"),
    "eye": (5253959125838090076, "👁", "👁"),
    "loading": (5258012149036365477, "◌", "📸"),
    "minus": (5275969776668134187, "−", "⛔️"),
    # objects
    "bot": (5258093637450866522, "🤖", "🤖"),
    "book": (5258328383183396223, "📖", "📖"),
    "bolt": (5258152182150077732, "⚡", "⚡️"),
    "users": (5258513401784573443, "👥", "👥"),
    "user": (5258011929993026890, "👤", "👤"),
    "user_add": (5258362837411045098, "👤", "👤"),
    "account": (5260399854500191689, "👤", "👤"),
    "phone": (5258337316715373336, "📞", "🤙"),
    "camera": (5258205968025525531, "📷", "📸"),
    "file": (5257965810634202885, "📄", "📁"),
    "folder": (5257969839313526622, "📁", "📂"),
    "apps": (5226513232549664618, "🧩", "🔢"),
    "info": (5258503720928288433, "ℹ️", "ℹ️"),
    "warning": (5258474669769497337, "⚠️", "❗️"),
    "clock": (5258419835922030550, "🕒", "🕔"),
    "timer": (5258258882022612173, "⏱", "⏲"),
    "stats": (5258330865674494479, "📊", "🍑"),
    "chart": (5258391025281408576, "📈", "📈"),
    "translate": (5260512129240276089, "🌐", "📚"),
    "tag": (5296385246579670377, "🏷", "🏷"),
    "pin": (5258461531464539536, "📌", "📌"),
    "star": (5258185631355378853, "☆", "⭐️"),
    "star_fill": (5258165702707125574, "★", "⭐️"),
    "premium": (5280962371207077415, "💎", "💎"),
    "idea": (5258216851472654189, "💡", "💡"),
    "home": (5257963315258204021, "🏠", "🏘"),
    "send": (5258073068852485953, "✈️", "✈️"),
    "checkbox": (5258453452631056344, "☑️", "❌"),
    "text": (5370546867786523009, "🔤", "📝"),
    # Hotaru set
    "hotaru": (5884064191467233197, "◉", "⚡️"),
    "hotaru_ho": (5886680238867357294, "HO", "⚡️"),
    "hotaru_ta": (5884059780535821183, "TA", "⚡️"),
    "hotaru_ru": (5884240418270356231, "RU", "⚡️"),
    "terminal": (5884429800558305287, "⌨️", "⚡️"),
}

_modules: Dict[str, str] = {
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
    "settings": "settings",
    "backups": "folder",
    "catalyst": "download",
}


def get(name: Optional[str]) -> Optional[Tuple[int, str, str]]:
    return _icons.get(name) if name else None


def emoji(name: Optional[str]) -> str:
    icon = get(name)
    if icon is None:
        return ""
    return f'<tg-emoji emoji-id="{icon[0]}">{html.escape(icon[2], quote=False)}</tg-emoji>'


def fallback(name: Optional[str]) -> str:
    icon = get(name)
    return icon[1] if icon is not None else ""


def wordmark() -> str:
    return "".join(emoji(name) for name in ("hotaru", "hotaru_ho", "hotaru_ta", "hotaru_ru"))


def module(module_id: str, default: str = "folder") -> str:
    return _modules.get(module_id, default)
