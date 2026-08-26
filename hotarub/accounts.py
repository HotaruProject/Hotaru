from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


ACCOUNT_VAULT_RE = re.compile(r"hotaru-[0-9a-f]{64}\.vault")


def vault_name(user_id: int, account_number: int) -> str:
    if user_id <= 0 or account_number <= 0:
        raise ValueError("user_id and account_number must be positive")
    digest = hashlib.sha256(f"{user_id}:{account_number}".encode("ascii")).hexdigest()
    return f"hotaru-{digest}.vault"


@dataclass(frozen=True, slots=True)
class AccountProfile:
    user_id: int
    account_number: int
    vault_name: str
    session_dir: Path
    enabled: bool = True

    @classmethod
    def create(cls, user_id: int, account_number: int, session_dir: str | Path) -> "AccountProfile":
        return cls(user_id, account_number, vault_name(user_id, account_number), Path(session_dir))

    def validate(self) -> None:
        if self.user_id <= 0 or self.account_number <= 0:
            raise ValueError("account identity must be positive")
        if not ACCOUNT_VAULT_RE.fullmatch(self.vault_name):
            raise ValueError("account vault name is invalid")
        if self.session_dir.name in {"", ".", ".."}:
            raise ValueError("account session directory is invalid")
