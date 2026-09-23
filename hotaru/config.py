from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, cast

from .accounts import account_db_path
from .state import StateNamespace, StateStore

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "sanctuary/state.sqlite3"


def discover_state(path: str | Path = DEFAULT_STATE_PATH) -> Path:
    bootstrap = Path(path)
    session_dir = Path(".")
    active = None
    if bootstrap.exists() and bootstrap.stat().st_size > 0:
        state = StateStore(bootstrap)
        try:
            raw = state.get_setting("session-dir")
            if raw:
                session_dir = Path(str(raw)).expanduser()
            active = state.get_setting("active-account")
        except Exception:
            pass
        finally:
            state.close()
    root = session_dir.expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    else:
        root = root.resolve()
    if isinstance(active, int) and active > 0:
        candidate = account_db_path(root, active)
        if candidate.is_file():
            return candidate
    found = sorted(root.glob("sanctuary/account-*/hotaru-*.sqlite3")) or sorted(root.glob("sanctuary/account-*/state-*.sqlite3")) or sorted(root.glob("account-*/hotaru-*.sqlite3")) or sorted(root.glob("account-*/state-*.sqlite3"))
    if found:
        return found[0]
    return bootstrap


class ConfigError(ValueError):
    pass


class ConfigValidationError(ConfigError):
    pass


class ConfigNotFoundError(ConfigError):
    pass


class Validator:
    name: str = "any"
    default: Any = None

    def validate(self, value: Any) -> Any:
        return value

    def parse(self, text: str) -> Any:
        return self.validate(text)

    def to_string(self, value: Any) -> str:
        return str(value)

    def to_json(self, value: Any) -> Any:
        return value

    def schema_dict(self) -> dict[str, Any]:
        return {"type": self.name, "default": self.default}


class BooleanValidator(Validator):
    name: str = "bool"

    def __init__(self, default: bool = False) -> None:
        self.default = bool(default)

    def validate(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            norm = value.strip().casefold()
            if norm in {"1", "true", "yes", "y", "on", "enable", "enabled", "t"}:
                return True
            if norm in {"0", "false", "no", "n", "off", "disable", "disabled", "f"}:
                return False
            raise ConfigValidationError(f"Invalid boolean value: {value!r}")
        raise ConfigValidationError(f"Expected boolean, got {type(value).__name__}")

    def parse(self, text: str) -> bool:
        return self.validate(text)

    def to_string(self, value: Any) -> str:
        return "True" if bool(value) else "False"

    def to_json(self, value: Any) -> bool:
        return bool(value)

    def schema_dict(self) -> dict[str, Any]:
        return {"type": "bool", "default": self.default}


class IntegerValidator(Validator):
    name: str = "int"

    def __init__(self, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> None:
        self.default = int(default)
        self.minimum = minimum
        self.maximum = maximum

    def validate(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ConfigValidationError("Expected integer, got boolean")
        try:
            val = int(value)
        except (ValueError, TypeError):
            raise ConfigValidationError(f"Invalid integer value: {value!r}")
        if self.minimum is not None and val < self.minimum:
            raise ConfigValidationError(f"Value {val} is less than minimum {self.minimum}")
        if self.maximum is not None and val > self.maximum:
            raise ConfigValidationError(f"Value {val} is greater than maximum {self.maximum}")
        return val

    def parse(self, text: str) -> int:
        return self.validate(text.strip())

    def to_string(self, value: Any) -> str:
        return str(int(value))

    def to_json(self, value: Any) -> int:
        return int(value)

    def schema_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {"type": "int", "default": self.default}
        if self.minimum is not None:
            res["minimum"] = self.minimum
        if self.maximum is not None:
            res["maximum"] = self.maximum
        return res


class FloatValidator(Validator):
    name: str = "float"

    def __init__(self, default: float = 0.0, minimum: float | None = None, maximum: float | None = None) -> None:
        self.default = float(default)
        self.minimum = minimum
        self.maximum = maximum

    def validate(self, value: Any) -> float:
        if isinstance(value, bool):
            raise ConfigValidationError("Expected float, got boolean")
        try:
            val = float(value)
        except (ValueError, TypeError):
            raise ConfigValidationError(f"Invalid float value: {value!r}")
        if self.minimum is not None and val < self.minimum:
            raise ConfigValidationError(f"Value {val} is less than minimum {self.minimum}")
        if self.maximum is not None and val > self.maximum:
            raise ConfigValidationError(f"Value {val} is greater than maximum {self.maximum}")
        return val

    def parse(self, text: str) -> float:
        return self.validate(text.strip())

    def to_string(self, value: Any) -> str:
        return str(float(value))

    def to_json(self, value: Any) -> float:
        return float(value)

    def schema_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {"type": "float", "default": self.default}
        if self.minimum is not None:
            res["minimum"] = self.minimum
        if self.maximum is not None:
            res["maximum"] = self.maximum
        return res


class StringValidator(Validator):
    name: str = "str"

    def __init__(self, default: str = "", min_len: int | None = None, max_len: int | None = None, regex: str | None = None) -> None:
        self.default = str(default)
        self.min_len = min_len
        self.max_len = max_len
        self.regex = regex
        self._compiled = re.compile(regex) if regex is not None else None

    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            if isinstance(value, (int, float, bool)):
                value = str(value)
            else:
                raise ConfigValidationError(f"Expected string, got {type(value).__name__}")
        if self.min_len is not None and len(value) < self.min_len:
            raise ConfigValidationError(f"Length {len(value)} is shorter than minimum length {self.min_len}")
        if self.max_len is not None and len(value) > self.max_len:
            raise ConfigValidationError(f"Length {len(value)} exceeds maximum length {self.max_len}")
        if self._compiled is not None and not self._compiled.search(value):
            raise ConfigValidationError(f"Value does not match pattern {self.regex!r}")
        return value

    def parse(self, text: str) -> str:
        return self.validate(text)

    def to_string(self, value: Any) -> str:
        return str(value)

    def to_json(self, value: Any) -> str:
        return str(value)

    def schema_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {"type": "str", "default": self.default}
        if self.min_len is not None:
            res["min_len"] = self.min_len
        if self.max_len is not None:
            res["max_len"] = self.max_len
        if self.regex is not None:
            res["regex"] = self.regex
        return res


class ChoiceValidator(Validator):
    name: str = "choice"

    def __init__(self, options: Sequence[Any], default: Any = None) -> None:
        if not options:
            raise ValueError("ChoiceValidator requires non-empty options")
        self.options = tuple(options)
        self.default = default if default is not None else self.options[0]

    def validate(self, value: Any) -> Any:
        for opt in self.options:
            if opt == value or str(opt).casefold() == str(value).casefold():
                return opt
        opts = ", ".join(repr(opt) for opt in self.options)
        raise ConfigValidationError(f"Value {value!r} not in choices: [{opts}]")

    def parse(self, text: str) -> Any:
        return self.validate(text.strip())

    def to_string(self, value: Any) -> str:
        return str(value)

    def to_json(self, value: Any) -> Any:
        return value

    def schema_dict(self) -> dict[str, Any]:
        return {"type": "choice", "options": list(self.options), "default": self.default}


class SeriesValidator(Validator):
    name: str = "series"

    def __init__(self, validator: Validator, min_items: int | None = None, max_items: int | None = None, separator: str = ",", default: Sequence[Any] | None = None) -> None:
        self.item_validator = validator
        self.min_items = min_items
        self.max_items = max_items
        self.separator = separator
        self.default: list[Any] = list(default) if default is not None else []

    def validate(self, value: Any) -> list[Any]:
        if isinstance(value, (list, tuple, set)):
            items = list(cast("Sequence[object]", value))
        elif isinstance(value, str):
            return self.parse(value)
        else:
            raise ConfigValidationError(f"Expected list/series, got {type(value).__name__}")
        validated = [self.item_validator.validate(item) for item in items]
        if self.min_items is not None and len(validated) < self.min_items:
            raise ConfigValidationError(f"Series has {len(validated)} items, minimum is {self.min_items}")
        if self.max_items is not None and len(validated) > self.max_items:
            raise ConfigValidationError(f"Series has {len(validated)} items, maximum is {self.max_items}")
        return validated

    def parse(self, text: str) -> list[Any]:
        raw = text.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return self.validate(parsed)
            except Exception:
                pass
        parts = [part.strip() for part in raw.split(self.separator) if part.strip()]
        return [self.item_validator.parse(part) for part in parts]

    def to_string(self, value: Any) -> str:
        if isinstance(value, (list, tuple)):
            items = cast("Sequence[object]", value)
            return (self.separator + " ").join(self.item_validator.to_string(v) for v in items)
        return str(value)

    def to_json(self, value: Any) -> list[Any]:
        if isinstance(value, (list, tuple)):
            items = cast("Sequence[object]", value)
            return [self.item_validator.to_json(v) for v in items]
        return []

    def schema_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {"type": "series", "item_type": self.item_validator.schema_dict(), "default": self.default}
        if self.min_items is not None:
            res["min_items"] = self.min_items
        if self.max_items is not None:
            res["max_items"] = self.max_items
        return res


class SecretValidator(Validator):
    name: str = "secret"

    def __init__(self, validator: Validator | None = None, default: Any = "") -> None:
        self.inner = validator or StringValidator(default=str(default))
        self.default = default

    def validate(self, value: Any) -> Any:
        return self.inner.validate(value)

    def parse(self, text: str) -> Any:
        return self.inner.parse(text)

    def to_string(self, value: Any) -> str:
        return "••••••••" if value else "(empty)"

    def to_json(self, value: Any) -> Any:
        return self.inner.to_json(value)

    def schema_dict(self) -> dict[str, Any]:
        res = self.inner.schema_dict()
        res["secret"] = True
        return res


class UnionValidator(Validator):
    name: str = "union"

    def __init__(self, *validators: Validator, default: Any = None) -> None:
        if not validators:
            raise ValueError("UnionValidator requires at least one validator")
        self.validators = validators
        self.default = default if default is not None else validators[0].default

    def validate(self, value: Any) -> Any:
        errors: list[str] = []
        for v in self.validators:
            try:
                return v.validate(value)
            except ConfigValidationError as e:
                errors.append(str(e))
        raise ConfigValidationError(f"Value does not match any union type: {'; '.join(errors)}")

    def parse(self, text: str) -> Any:
        errors: list[str] = []
        for v in self.validators:
            try:
                return v.parse(text)
            except ConfigValidationError as e:
                errors.append(str(e))
        raise ConfigValidationError(f"Input cannot be parsed by union: {'; '.join(errors)}")

    def to_string(self, value: Any) -> str:
        for v in self.validators:
            try:
                v.validate(value)
                return v.to_string(value)
            except Exception:
                continue
        return str(value)

    def to_json(self, value: Any) -> Any:
        return value

    def schema_dict(self) -> dict[str, Any]:
        return {"type": "union", "options": [v.schema_dict() for v in self.validators], "default": self.default}


Boolean = BooleanValidator
Integer = IntegerValidator
Float = FloatValidator
String = StringValidator
Choice = ChoiceValidator
Series = SeriesValidator
Secret = SecretValidator
Union = UnionValidator


def validator_from_spec(spec: Any) -> Validator:
    if isinstance(spec, Validator):
        return spec
    if isinstance(spec, dict):
        spec_dict = cast(dict[str, Any], spec)
        type_name = str(spec_dict.get("type", "str")).casefold()
        default = spec_dict.get("default")
        if type_name in {"bool", "boolean"}:
            return BooleanValidator(default=bool(default))
        if type_name in {"int", "integer"}:
            minimum = spec_dict.get("minimum", spec_dict.get("min"))
            maximum = spec_dict.get("maximum", spec_dict.get("max"))
            return IntegerValidator(default=int(default or 0), minimum=int(minimum) if minimum is not None else None, maximum=int(maximum) if maximum is not None else None)
        if type_name in {"float"}:
            minimum = spec_dict.get("minimum", spec_dict.get("min"))
            maximum = spec_dict.get("maximum", spec_dict.get("max"))
            return FloatValidator(default=float(default or 0.0), minimum=float(minimum) if minimum is not None else None, maximum=float(maximum) if maximum is not None else None)
        if type_name in {"choice", "select"}:
            opts = spec_dict.get("options") or ()
            return ChoiceValidator(options=list(opts), default=default)
        if type_name in {"series", "list"}:
            item_spec = spec_dict.get("item") or {"type": "str"}
            sub_validator = validator_from_spec(item_spec)
            min_items = spec_dict.get("min_items")
            max_items = spec_dict.get("max_items")
            sep = str(spec_dict.get("separator", ","))
            def_seq = cast("Sequence[Any]", default) if isinstance(default, (list, tuple)) else None
            return SeriesValidator(validator=sub_validator, min_items=int(min_items) if min_items is not None else None, max_items=int(max_items) if max_items is not None else None, separator=sep, default=def_seq)
        if bool(spec_dict.get("secret", False)):
            sub = StringValidator(default=str(default or ""))
            return SecretValidator(sub, default=default)
        min_len = spec_dict.get("min_len", spec_dict.get("min_length"))
        max_len = spec_dict.get("max_len", spec_dict.get("max_length"))
        regex = spec_dict.get("regex")
        return StringValidator(default=str(default or ""), min_len=int(min_len) if min_len is not None else None, max_len=int(max_len) if max_len is not None else None, regex=str(regex) if regex is not None else None)
    return StringValidator(default=str(spec or ""))


@dataclass(frozen=True)
class ConfigField:
    key: str
    validator: Validator
    default: Any
    description: str = ""
    category: str = "General"
    doc: str = ""
    secret: bool = False
    requires_restart: bool = False

    def coerce(self, value: Any) -> Any:
        return self.validator.validate(value)

    def parse_input(self, raw: str) -> Any:
        return self.validator.parse(raw)

    def format_value(self, value: Any, mask_secret: bool = True) -> str:
        if self.secret and mask_secret:
            return "••••••••" if value else "(empty)"
        return self.validator.to_string(value)

    def to_dict(self) -> dict[str, Any]:
        res = self.validator.schema_dict()
        res.update({
            "key": self.key,
            "default": self.default,
            "description": self.description,
            "category": self.category,
            "doc": self.doc,
            "secret": self.secret,
            "requires_restart": self.requires_restart,
        })
        return res


class ConfigSchema:
    def __init__(self, fields: Mapping[str, ConfigField]) -> None:
        self._fields = dict(fields)

    @classmethod
    def from_manifest(cls, raw: Any) -> "ConfigSchema":
        if isinstance(raw, ConfigSchema):
            return raw
        fields: dict[str, ConfigField] = {}
        if isinstance(raw, dict):
            raw_dict = cast("dict[object, object]", raw)
            for k, s in raw_dict.items():
                key = str(k)
                if isinstance(s, ConfigField):
                    fields[key] = s
                elif isinstance(s, Validator):
                    fields[key] = ConfigField(key=key, validator=s, default=s.default)
                elif isinstance(s, dict):
                    spec = cast("dict[str, object]", s)
                    v = validator_from_spec(spec)
                    default = spec.get("default", v.default)
                    desc = str(spec.get("description", ""))
                    cat = str(spec.get("category", "General"))
                    doc = str(spec.get("doc", ""))
                    sec = bool(spec.get("secret", False) or isinstance(v, SecretValidator))
                    rst = bool(spec.get("requires_restart", False))
                    fields[key] = ConfigField(key=key, validator=v, default=default, description=desc, category=cat, doc=doc, secret=sec, requires_restart=rst)
                else:
                    v = StringValidator(default=str(s or ""))
                    fields[key] = ConfigField(key=key, validator=v, default=str(s or ""))
        return cls(fields)

    def get(self, key: str) -> ConfigField | None:
        return self._fields.get(key)

    def __getitem__(self, key: str) -> ConfigField:
        if key not in self._fields:
            raise ConfigNotFoundError(f"Config field {key!r} not found in schema")
        return self._fields[key]

    def __contains__(self, key: str) -> bool:
        return key in self._fields

    def __iter__(self) -> Iterator[str]:
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def items(self) -> tuple[tuple[str, ConfigField], ...]:
        return tuple(self._fields.items())

    def categories(self) -> list[str]:
        cats = sorted({field.category for field in self._fields.values() if field.category})
        if "General" in cats:
            cats.remove("General")
            cats.insert(0, "General")
        return cats or ["General"]

    def fields_in_category(self, category: str) -> list[ConfigField]:
        return [f for f in self._fields.values() if f.category == category]

    def validate(self, key: str, value: Any) -> Any:
        field = self.get(key)
        if field is None:
            return value
        return field.coerce(value)

    def parse(self, key: str, raw: str) -> Any:
        field = self.get(key)
        if field is None:
            return raw
        return field.parse_input(raw)

    def defaults(self) -> dict[str, Any]:
        return {key: field.default for key, field in self._fields.items()}

    def to_dict(self) -> dict[str, Any]:
        return {key: field.to_dict() for key, field in self._fields.items()}


class ModuleConfig:
    def __init__(self, module_id: str, state: StateNamespace, schema: ConfigSchema | Mapping[str, Any] | None = None) -> None:
        self.module_id = module_id
        self.state = state
        self.schema = ConfigSchema.from_manifest(schema) if schema is not None else ConfigSchema({})

    def get(self, key: str, default: Any = None) -> Any:
        field = self.schema.get(key)
        fallback = field.default if field is not None else default
        raw = self.state.get(key, fallback)
        if raw is None and fallback is not None:
            raw = fallback
        if field is not None and raw is not None:
            try:
                return field.coerce(raw)
            except ConfigValidationError:
                return fallback
        return raw

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None and key not in self.schema:
            raise KeyError(f"Key {key!r} not configured in module {self.module_id}")
        return val

    def set(self, key: str, value: Any) -> Any:
        field = self.schema.get(key)
        coerced = field.coerce(value) if field is not None else value
        self.state.set(key, coerced)
        return coerced

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def delete(self, key: str) -> bool:
        return self.state.delete(key)

    def reset(self, key: str | None = None) -> None:
        if key is not None:
            field = self.schema.get(key)
            if field is not None:
                self.set(key, field.default)
            else:
                self.delete(key)
        else:
            for k, field in self.schema.items():
                self.set(k, field.default)

    def all(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for key in self.schema:
            res[key] = self.get(key)
        for key in self.state.keys():
            if key not in res:
                res[key] = self.state.get(key)
        return res

    def diff(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, field in self.schema.items():
            current = self.get(key)
            if current != field.default:
                result[key] = {
                    "current": current,
                    "default": field.default,
                    "category": field.category,
                }
        return result

    def export_dict(self, mask_secrets: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, val in self.all().items():
            field = self.schema.get(key)
            if field is not None and field.secret and mask_secrets:
                data[key] = "••••••••"
            else:
                data[key] = val
        return data

    def import_dict(self, data: Mapping[str, Any]) -> tuple[int, list[str]]:
        count = 0
        errors: list[str] = []
        for k, val in data.items():
            key = str(k)
            if val == "••••••••":
                continue
            try:
                self.set(key, val)
                count += 1
            except Exception as e:
                errors.append(f"{key}: {e}")
        return count, errors

    def categories(self) -> list[str]:
        return self.schema.categories()

    def fields_by_category(self) -> dict[str, list[ConfigField]]:
        return {cat: self.schema.fields_in_category(cat) for cat in self.categories()}


def parse_dotenv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        result[key] = val
    return result


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    targets: list[Path] = []
    if path is not None:
        targets.append(Path(path))
    else:
        cwd = Path.cwd()
        targets.extend([cwd / ".env", cwd / "sanctuary" / ".env", Path(__file__).resolve().parent.parent / ".env"])
    merged: dict[str, str] = {}
    for p in targets:
        if p.is_file():
            try:
                merged.update(parse_dotenv(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    return merged


_ENV_MAPPINGS: dict[str, tuple[str, ...]] = {
    "api-id": ("HOTARU_API_ID", "API_ID", "TG_API_ID"),
    "api-hash": ("HOTARU_API_HASH", "API_HASH", "TG_API_HASH"),
    "bot-token": ("HOTARU_BOT_TOKEN", "BOT_TOKEN", "TG_BOT_TOKEN"),
    "owner-id": ("HOTARU_OWNER_ID", "OWNER_ID", "TG_OWNER_ID"),
    "prefix": ("HOTARU_PREFIX", "PREFIX"),
    "session-name": ("HOTARU_SESSION_NAME", "SESSION_NAME"),
    "session-dir": ("HOTARU_SESSION_DIR", "SESSION_DIR"),
    "backup-keep": ("HOTARU_BACKUP_KEEP", "BACKUP_KEEP"),
    "command-timeout": ("HOTARU_COMMAND_TIMEOUT", "COMMAND_TIMEOUT"),
    "inline-enabled": ("HOTARU_INLINE_ENABLED", "INLINE_ENABLED"),
    "log-level": ("HOTARU_LOG_LEVEL", "LOG_LEVEL"),
    "environment": ("HOTARU_ENV", "ENVIRONMENT", "NODE_ENV"),
}


def _resolve_env(key: str, dotenv: dict[str, str]) -> str | None:
    aliases = _ENV_MAPPINGS.get(key, ())
    for alias in aliases:
        if alias in os.environ and os.environ[alias].strip():
            return os.environ[alias].strip()
        if alias in dotenv and dotenv[alias].strip():
            return dotenv[alias].strip()
    return None


@dataclass(frozen=True)
class RuntimeConfig:
    api_id: int | None
    api_hash: str | None
    bot_token: str | None
    owner_id: int | None
    prefix: str
    session_name: str
    session_dir: Path
    state_path: Path
    backup_keep: int
    command_timeout: float = 60.0
    inline_enabled: bool = True
    log_level: str = "INFO"
    environment: str = "production"

    @classmethod
    def from_database(cls, path: str | Path = DEFAULT_STATE_PATH) -> "RuntimeConfig":
        path = discover_state(path)
        dotenv = load_dotenv()
        state = StateStore(path)
        try:
            values: dict[str, Any] = {}
            for setting_key in ("api-id", "api-hash", "bot-token", "owner-id", "prefix", "session-name", "session-dir", "backup-keep", "command-timeout", "inline-enabled", "log-level", "environment"):
                raw_env = _resolve_env(setting_key, dotenv)
                if raw_env is not None:
                    if setting_key == "api-id":
                        try:
                            val = int(raw_env)
                        except ValueError:
                            val = None
                    elif setting_key in {"owner-id", "backup-keep"}:
                        try:
                            val = int(raw_env)
                        except ValueError:
                            val = None
                    elif setting_key == "command-timeout":
                        try:
                            val = float(raw_env)
                        except ValueError:
                            val = 60.0
                    elif setting_key == "inline-enabled":
                        try:
                            val = BooleanValidator().validate(raw_env)
                        except ConfigValidationError:
                            val = True
                    elif setting_key == "log-level":
                        val = raw_env.upper()
                    elif setting_key == "environment":
                        val = raw_env.lower()
                    else:
                        val = raw_env
                    values[setting_key] = val
                    if state.get_setting(setting_key) is None and val is not None:
                        state.set_setting(setting_key, val)
                else:
                    values[setting_key] = state.get_setting(setting_key)

            if values.get("prefix") is None:
                values["prefix"] = "!"
                state.set_setting("prefix", "!")
            if values.get("session-name") is None:
                values["session-name"] = "hotaru"
                state.set_setting("session-name", "hotaru")
            if values.get("session-dir") is None:
                values["session-dir"] = "sanctuary"
                state.set_setting("session-dir", "sanctuary")
            if values.get("backup-keep") is None:
                values["backup-keep"] = 7
                state.set_setting("backup-keep", 7)
            if values.get("command-timeout") is None:
                values["command-timeout"] = 60.0
            if values.get("inline-enabled") is None:
                values["inline-enabled"] = True
            if values.get("log-level") is None:
                values["log-level"] = "INFO"
            if values.get("environment") is None:
                values["environment"] = "production"

            if values.get("api-id") is None and values.get("bot-token") is None:
                if sys.stdin.isatty() and sys.stdout.isatty():
                    from .login import collect_settings
                    collect_settings(state)
                    values["api-id"] = state.get_setting("api-id")
                    values["api-hash"] = state.get_setting("api-hash")
                    values["prefix"] = state.get_setting("prefix") or "!"
                    values["session-name"] = state.get_setting("session-name") or "hotaru"
                    values["session-dir"] = state.get_setting("session-dir") or "sanctuary"
                    values["backup-keep"] = state.get_setting("backup-keep") or 7
                else:
                    raise ConfigError("Missing Telegram credentials. Set API_ID and API_HASH (or BOT_TOKEN) in environment / Heroku Config Vars.")

            return cls(
                api_id=int(values["api-id"]) if values["api-id"] is not None else None,
                api_hash=str(values["api-hash"]) if values["api-hash"] is not None else None,
                bot_token=str(values["bot-token"]) if values["bot-token"] is not None else None,
                owner_id=int(values["owner-id"]) if values["owner-id"] is not None else None,
                prefix=str(values["prefix"]),
                session_name=str(values["session-name"]),
                session_dir=Path(str(values["session-dir"])),
                state_path=Path(path),
                backup_keep=int(values["backup-keep"]),
                command_timeout=float(values["command-timeout"]),
                inline_enabled=bool(values["inline-enabled"]),
                log_level=str(values["log-level"]),
                environment=str(values["environment"]),
            )
        finally:
            state.close()

    def validate(self) -> None:
        if (self.api_id is None) != (self.api_hash is None):
            raise ValueError("API ID and API hash must be configured together")
        if self.api_hash is not None and not self.api_hash.strip():
            raise ValueError("API hash must not be empty")
        if self.bot_token is not None and not self.bot_token.strip():
            raise ValueError("bot token must not be empty")
        if self.api_id is None and self.bot_token is None:
            raise ValueError("configure MTProto credentials or bot token in the database")
        if len(self.prefix) != 1 or self.prefix.isspace():
            raise ValueError("prefix must be one non-whitespace character")
        if self.owner_id is not None and self.owner_id == 0:
            raise ValueError("owner ID must be nonzero")
        if self.backup_keep < 1:
            raise ValueError("backup retention must be positive")
        if not self.session_name or Path(self.session_name).name != self.session_name:
            raise ValueError("session name must be a simple filename")
        if self.api_id is not None and self.api_id <= 0:
            raise ValueError("API ID must be positive")

    def as_dict(self, mask_secrets: bool = True) -> dict[str, Any]:
        return {
            "api_id": self.api_id,
            "api_hash": "••••••••" if self.api_hash and mask_secrets else self.api_hash,
            "bot_token": "••••••••" if self.bot_token and mask_secrets else self.bot_token,
            "owner_id": self.owner_id,
            "prefix": self.prefix,
            "session_name": self.session_name,
            "session_dir": str(self.session_dir),
            "state_path": str(self.state_path),
            "backup_keep": self.backup_keep,
            "command_timeout": self.command_timeout,
            "inline_enabled": self.inline_enabled,
            "log_level": self.log_level,
            "environment": self.environment,
        }

    def to_env(self, mask_secrets: bool = True) -> str:
        lines: list[str] = []
        if self.api_id is not None:
            lines.append(f"API_ID={self.api_id}")
        if self.api_hash is not None:
            val = "••••••••" if mask_secrets else self.api_hash
            lines.append(f"API_HASH={val}")
        if self.bot_token is not None:
            val = "••••••••" if mask_secrets else self.bot_token
            lines.append(f"BOT_TOKEN={val}")
        if self.owner_id is not None:
            lines.append(f"OWNER_ID={self.owner_id}")
        lines.append(f"PREFIX={self.prefix}")
        lines.append(f"SESSION_NAME={self.session_name}")
        lines.append(f"SESSION_DIR={self.session_dir}")
        lines.append(f"BACKUP_KEEP={self.backup_keep}")
        lines.append(f"COMMAND_TIMEOUT={self.command_timeout}")
        lines.append(f"INLINE_ENABLED={str(self.inline_enabled).lower()}")
        lines.append(f"LOG_LEVEL={self.log_level}")
        lines.append(f"ENVIRONMENT={self.environment}")
        return "\n".join(lines)

    def update_setting(self, state: StateStore, key: str, value: Any) -> "RuntimeConfig":
        state.set_setting(key, value)
        return RuntimeConfig.from_database(self.state_path)


CORE_SCHEMA = ConfigSchema({
    "prefix": ConfigField(
        key="prefix",
        validator=StringValidator(default="!", min_len=1, max_len=1),
        default="!",
        description="Command prefix character",
        category="General",
    ),
    "backup-keep": ConfigField(
        key="backup-keep",
        validator=IntegerValidator(default=7, minimum=1, maximum=100),
        default=7,
        description="Number of database backups to retain",
        category="Maintenance",
    ),
    "command-timeout": ConfigField(
        key="command-timeout",
        validator=FloatValidator(default=60.0, minimum=1.0, maximum=600.0),
        default=60.0,
        description="Maximum seconds before command execution times out",
        category="Execution",
    ),
    "inline-enabled": ConfigField(
        key="inline-enabled",
        validator=BooleanValidator(default=True),
        default=True,
        description="Enable inline query bot features and forms",
        category="General",
    ),
})


class ConfigManager:
    @staticmethod
    def search(runtime: Any, query: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        term = query.strip().casefold()
        if not term:
            return results

        for key, field in CORE_SCHEMA.items():
            if term in key.casefold() or term in field.description.casefold() or term in field.category.casefold():
                val = getattr(runtime.config, key.replace("-", "_"), None)
                results.append({
                    "type": "core",
                    "module_id": "core",
                    "key": key,
                    "field": field,
                    "current": val,
                })

        modules = getattr(runtime, "modules", None)
        if modules is not None and hasattr(modules, "items"):
            for active in modules.items():
                if not getattr(active, "loaded", None):
                    continue
                manifest = getattr(active.loaded, "manifest", None)
                if manifest is None:
                    continue
                schema_raw = getattr(manifest, "config_schema", None)
                if not schema_raw:
                    continue
                schema = ConfigSchema.from_manifest(schema_raw)
                ns = runtime.state.namespace(manifest.module_id)
                mod_conf = ModuleConfig(manifest.module_id, ns, schema)
                for key, field in schema.items():
                    if (
                        term in manifest.module_id.casefold()
                        or term in key.casefold()
                        or term in field.description.casefold()
                        or term in field.category.casefold()
                    ):
                        results.append({
                            "type": "module",
                            "module_id": manifest.module_id,
                            "key": key,
                            "field": field,
                            "current": mod_conf.get(key),
                        })
        return results

    @staticmethod
    def export_all(runtime: Any, mask_secrets: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "core": runtime.config.as_dict(mask_secrets=mask_secrets),
            "modules": {},
        }
        modules = getattr(runtime, "modules", None)
        if modules is not None and hasattr(modules, "items"):
            for active in modules.items():
                if not getattr(active, "loaded", None):
                    continue
                manifest = getattr(active.loaded, "manifest", None)
                if manifest is None or not getattr(manifest, "config_schema", None):
                    continue
                schema = ConfigSchema.from_manifest(manifest.config_schema)
                ns = runtime.state.namespace(manifest.module_id)
                mod_conf = ModuleConfig(manifest.module_id, ns, schema)
                data["modules"][manifest.module_id] = mod_conf.export_dict(mask_secrets=mask_secrets)
        return data

    @staticmethod
    def import_all(runtime: Any, data: Mapping[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = {"core": 0, "modules": {}, "errors": []}
        modules_data = data.get("modules")
        if isinstance(modules_data, dict):
            raw_modules = cast("dict[object, object]", modules_data)
            modules = getattr(runtime, "modules", None)
            for mod_k, mod_vals_obj in raw_modules.items():
                if not isinstance(mod_vals_obj, dict) or modules is None:
                    continue
                mod_id = str(mod_k)
                mod_vals = cast("dict[str, Any]", mod_vals_obj)
                active = modules.get(mod_id)
                if active is None or not getattr(active, "loaded", None):
                    continue
                manifest = getattr(active.loaded, "manifest", None)
                if manifest is None or not getattr(manifest, "config_schema", None):
                    continue
                schema = ConfigSchema.from_manifest(manifest.config_schema)
                ns = runtime.state.namespace(manifest.module_id)
                mod_conf = ModuleConfig(manifest.module_id, ns, schema)
                count, errs = mod_conf.import_dict(mod_vals)
                report["modules"][mod_id] = count
                report["errors"].extend([f"{mod_id}.{e}" for e in errs])
        return report
