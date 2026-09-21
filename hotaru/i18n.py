from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

SUPPORTED_LANGUAGES = ("ru", "en", "kz", "uk", "ja")
DEFAULT_LANGUAGE = "en"
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class TranslationError(ValueError):
    pass


def _object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in cast('dict[object, object]', value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


class Lexicon:
    def __init__(self, root: str | Path, default_language: str = DEFAULT_LANGUAGE) -> None:
        self.root = Path(root)
        self.default_language = self.validate_language(default_language)
        self._cache: dict[str, dict[str, object]] = {}

    def available_languages(self) -> tuple[str, ...]:
        return tuple(language for language in SUPPORTED_LANGUAGES if (self.root / f"{language}.json").is_file())

    def load(self, language: str) -> dict[str, object]:
        language = self.validate_language(language)
        if language in self._cache:
            return self._cache[language]
        path = self.root / f"{language}.json"
        try:
            decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranslationError(f"locale cannot be loaded: {language}") from exc
        if not isinstance(decoded, dict):
            raise TranslationError(f"locale root must be an object: {language}")
        payload = cast('dict[str, object]', decoded)
        meta = payload.get("meta")
        if meta is not None and (not isinstance(meta, dict) or cast('dict[str, object]', meta).get("language", language) != language):
            raise TranslationError(f"locale metadata is invalid: {language}")
        self._validate_values(payload)
        self._cache[language] = payload
        return payload

    def has(self, language: str, key: str) -> bool:
        return self._lookup(self.load(language), key) is not None

    def get(self, locale: str, key: str, default: Any = None, **params: Any) -> str:
        if not key:
            raise TranslationError("translation key must be a non-empty string")
        languages: list[str] = []
        if locale.casefold() in SUPPORTED_LANGUAGES:
            languages.append(locale.casefold())
        for extra in (self.default_language, "ru", "en"):
            if extra not in languages:
                languages.append(extra)
        value = None
        for candidate in languages:
            try:
                found = self._lookup(self.load(candidate), key)
            except TranslationError:
                continue
            if isinstance(found, str):
                value = found
                break
        if value is None:
            value = default if isinstance(default, str) else key
        try:
            return value.format(**params)
        except (KeyError, IndexError, ValueError):
            return default if isinstance(default, str) else value

    def bundle(self, language: str) -> dict[str, object]:
        base = self.load(self.default_language)
        selected = self.load(language)

        def merge(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
            result = dict(left)
            for key, value in right.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key] = merge(cast('dict[str, object]', result[key]), cast('dict[str, object]', value))
                else:
                    result[key] = value
            return result

        return merge(base, selected)

    @staticmethod
    def _lookup(payload: dict[str, object], key: str) -> object:
        value: object = payload
        for part in key.split("."):
            mapping = _object_mapping(value)
            if mapping is None:
                return None
            if part not in mapping:
                return None
            value = mapping[part]
        return value

    @staticmethod
    def validate_language(language: str) -> str:
        if language.casefold() not in SUPPORTED_LANGUAGES:
            raise TranslationError(f"unsupported language: {language}")
        return language.casefold()

    @classmethod
    def _validate_values(cls, value: object) -> None:
        if isinstance(value, dict):
            for key, item in cast('dict[object, object]', value).items():
                if not isinstance(key, str) or not key:
                    raise TranslationError("locale contains an invalid key")
                cls._validate_values(item)
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            raise TranslationError("locale contains an invalid value")


class Translator:
    def __init__(self, lexicon: Lexicon, language: str) -> None:
        self.lexicon = lexicon
        self.language = lexicon.validate_language(language)

    def t(self, key: str, default: str | None = None, **params: Any) -> str:
        return self.lexicon.get(self.language, key, default, **params)

    def __call__(self, key: str, default: str | None = None, **params: Any) -> str:
        return self.t(key, default, **params)

    def has(self, key: str) -> bool:
        return self.lexicon.has(self.language, key)

    def with_language(self, language: str) -> "Translator":
        return Translator(self.lexicon, language)
