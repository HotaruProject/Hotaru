from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

SUPPORTED_LANGUAGES = ("ru", "en", "kz", "uk", "ja", "zh")
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
        self.custom_root = self.root / "custom"
        self.custom_root.mkdir(parents=True, exist_ok=True)
        self.default_language = self.validate_language(default_language)
        self._cache: dict[str, dict[str, object]] = {}

    def available_languages(self) -> tuple[str, ...]:
        builtins = {f.stem.casefold() for f in self.root.glob("*.json")}
        customs: set[str] = {f.stem.casefold() for f in self.custom_root.glob("*.json")} if self.custom_root.is_dir() else set()
        known = [lang for lang in SUPPORTED_LANGUAGES if lang in builtins]
        extra = sorted((builtins | customs) - set(SUPPORTED_LANGUAGES))
        return tuple(known + extra)

    def custom_languages(self) -> tuple[str, ...]:
        if not self.custom_root.is_dir():
            return ()
        return tuple(sorted(f.stem.casefold() for f in self.custom_root.glob("*.json")))

    def path_for(self, language: str) -> Path | None:
        code = language.casefold().strip()
        custom = self.custom_root / f"{code}.json"
        if custom.is_file():
            return custom
        standard = self.root / f"{code}.json"
        if standard.is_file():
            return standard
        return None

    def load(self, language: str) -> dict[str, object]:
        language = self.validate_language(language)
        if language in self._cache:
            return self._cache[language]
        path = self.path_for(language) or (self.root / f"{language}.json")
        try:
            decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TranslationError(f"locale cannot be loaded: {language}") from exc
        if not isinstance(decoded, dict):
            raise TranslationError(f"locale root must be an object: {language}")
        payload = cast('dict[str, object]', decoded)
        meta = payload.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise TranslationError(f"locale metadata is invalid: {language}")
        self._validate_values(payload)
        self._cache[language] = payload
        return payload

    def clear_cache(self, language: str | None = None) -> None:
        if language:
            self._cache.pop(language.casefold().strip(), None)
        else:
            self._cache.clear()

    def get_meta(self, language: str) -> dict[str, str]:
        try:
            payload = self.load(language)
            meta = payload.get("meta")
            if isinstance(meta, dict):
                meta_dict = cast('dict[str, Any]', meta)
                code = str(meta_dict.get("code") or meta_dict.get("language") or language).strip().lower()
                name_en = str(meta_dict.get("name_en") or meta_dict.get("name") or code.upper()).strip()
                name_native = str(meta_dict.get("name_native") or meta_dict.get("name") or name_en).strip()
                return {
                    "code": code,
                    "language": code,
                    "name": name_native,
                    "name_en": name_en,
                    "name_native": name_native,
                }
        except Exception:
            pass
        clean = language.casefold().strip()
        return {
            "code": clean,
            "language": clean,
            "name": clean.upper(),
            "name_en": clean.upper(),
            "name_native": clean.upper(),
        }

    @staticmethod
    def collect_leaf_keys(data: dict[str, object], prefix: str = "") -> set[str]:
        keys: set[str] = set()
        for k, v in data.items():
            if k == "meta":
                continue
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys.update(Lexicon.collect_leaf_keys(cast('dict[str, object]', v), path))
            else:
                keys.add(path)
        return keys

    def validate_coverage(self, custom_data: dict[str, object]) -> tuple[bool, set[str]]:
        base = self.load(self.default_language)
        base_keys = self.collect_leaf_keys(base)
        custom_keys = self.collect_leaf_keys(custom_data)
        missing = base_keys - custom_keys
        return len(missing) == 0, missing

    def remove_custom(self, code: str) -> bool:
        clean = code.casefold().strip()
        path = self.custom_root / f"{clean}.json"
        if not path.is_file():
            return False
        path.unlink(missing_ok=True)
        self.clear_cache(clean)
        return True

    def has(self, language: str, key: str) -> bool:
        return self._lookup(self.load(language), key) is not None

    def get(self, locale: str, key: str, /, default: Any = None, **params: Any) -> str:
        if not key:
            raise TranslationError("translation key must be a non-empty string")
        languages: list[str] = [locale.casefold()]
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

    def validate_language(self, language: str) -> str:
        code = str(language).casefold().strip()
        if code in SUPPORTED_LANGUAGES or (self.custom_root / f"{code}.json").is_file() or (self.root / f"{code}.json").is_file() or bool(re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{2,8})*", code)):
            return code
        raise TranslationError(f"unsupported language: {language}")

    def _validate_language(self, language: object) -> bool:
        if not isinstance(language, str):
            return False
        code = language.casefold().strip()
        return code in SUPPORTED_LANGUAGES or (self.custom_root / f"{code}.json").is_file() or (self.root / f"{code}.json").is_file() or bool(re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{2,8})*", code))

    is_valid_language = _validate_language

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

    def t(self, key: str, /, default: str | None = None, **params: Any) -> str:
        return self.lexicon.get(self.language, key, default, **params)

    def __call__(self, key: str, default: str | None = None, **params: Any) -> str:
        return self.t(key, default, **params)

    def has(self, key: str) -> bool:
        return self.lexicon.has(self.language, key)

    def with_language(self, language: str) -> "Translator":
        return Translator(self.lexicon, language)
