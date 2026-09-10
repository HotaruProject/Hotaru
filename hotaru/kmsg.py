from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .state import StateStore


KEY = "kmsg:entries"
MAX_ENTRIES = 32


class KernelMessageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KernelMessageRecord:
    slot: str
    module_id: str
    chat_id: int
    message_id: int | None
    text: str
    meta: dict[str, Any]
    created_at: float
    updated_at: float


class KernelMessageService:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def _load(self) -> dict[str, dict[str, Any]]:
        raw = self.state.get_setting(KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        if len(entries) > MAX_ENTRIES:
            ordered = sorted(entries.values(), key=lambda item: item.get("updated_at", 0.0))
            for stale in ordered[: len(entries) - MAX_ENTRIES]:
                entries.pop(stale.get("slot", ""), None)
        self.state.set_setting(KEY, entries)

    def put(self, slot: str, module_id: str, chat_id: int, message_id: int | None, text: str, meta: dict[str, Any] | None = None) -> KernelMessageRecord:
        if not isinstance(slot, str) or not slot or len(slot) > 64:
            raise KernelMessageError("kernel message slot is invalid")
        if not isinstance(chat_id, int):
            raise KernelMessageError("kernel message chat id is invalid")
        entries = self._load()
        now = time.time()
        previous = entries.get(slot)
        record = {
            "slot": slot,
            "module_id": module_id,
            "chat_id": chat_id,
            "message_id": message_id if isinstance(message_id, int) else (previous or {}).get("message_id"),
            "text": text,
            "meta": meta if isinstance(meta, dict) else (previous or {}).get("meta", {}),
            "created_at": (previous or {}).get("created_at", now),
            "updated_at": now,
        }
        entries[slot] = record
        self._save(entries)
        return KernelMessageRecord(
            slot=record["slot"],
            module_id=record["module_id"],
            chat_id=record["chat_id"],
            message_id=record["message_id"],
            text=record["text"],
            meta=record["meta"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    def get(self, slot: str) -> KernelMessageRecord | None:
        record = self._load().get(slot)
        if not isinstance(record, dict):
            return None
        return KernelMessageRecord(
            slot=record.get("slot", slot),
            module_id=str(record.get("module_id", "")),
            chat_id=record.get("chat_id", 0),
            message_id=record.get("message_id"),
            text=record.get("text", ""),
            meta=record.get("meta", {}),
            created_at=record.get("created_at", 0.0),
            updated_at=record.get("updated_at", 0.0),
        )

    def pop(self, slot: str) -> KernelMessageRecord | None:
        entries = self._load()
        record = entries.pop(slot, None)
        if record is None:
            return None
        self._save(entries)
        return KernelMessageRecord(
            slot=record.get("slot", slot),
            module_id=str(record.get("module_id", "")),
            chat_id=record.get("chat_id", 0),
            message_id=record.get("message_id"),
            text=record.get("text", ""),
            meta=record.get("meta", {}),
            created_at=record.get("created_at", 0.0),
            updated_at=record.get("updated_at", 0.0),
        )

    def items(self) -> tuple[KernelMessageRecord, ...]:
        entries = self._load()
        records = []
        for slot, record in sorted(entries.items()):
            if not isinstance(record, dict):
                continue
            records.append(KernelMessageRecord(
                slot=record.get("slot", slot),
                module_id=str(record.get("module_id", "")),
                chat_id=record.get("chat_id", 0),
                message_id=record.get("message_id"),
                text=record.get("text", ""),
                meta=record.get("meta", {}),
                created_at=record.get("created_at", 0.0),
                updated_at=record.get("updated_at", 0.0),
            ))
        return tuple(records)

    def of_module(self, module_id: str) -> tuple[KernelMessageRecord, ...]:
        return tuple(record for record in self.items() if record.module_id == module_id)

    def drop_module(self, module_id: str) -> int:
        entries = self._load()
        stale = [slot for slot, record in entries.items() if isinstance(record, dict) and record.get("module_id") == module_id]
        for slot in stale:
            entries.pop(slot, None)
        if stale:
            self._save(entries)
        return len(stale)

    @staticmethod
    async def edit(bot: Any, record: KernelMessageRecord, text: str) -> bool:
        if not isinstance(record.message_id, int) or not isinstance(record.chat_id, int):
            return False
        try:
            await bot.call("editMessageText", chat_id=record.chat_id, message_id=record.message_id, text=text, parse_mode="HTML")
            return True
        except Exception:
            return False
