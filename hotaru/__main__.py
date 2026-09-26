from __future__ import annotations

from typing import Any
import argparse
import asyncio
import sys


def ensure_kernel_dependencies() -> None:
    from .boot import in_venv, maybe_reexec
    from .deps import DependencyError, ensure_project
    if not in_venv():
        maybe_reexec()
        if not in_venv():
            raise SystemExit("run python -m hotaru from the repo so .venv can be created")
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    try:
        synced = ensure_project(root)
    except DependencyError as exc:
        print(f"dependency error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if synced.get("changed"):
        import os
        os.execv(sys.executable, [sys.executable, "-m", "hotaru", *sys.argv[1:]])


def _apply_account_session(config: Any, session_base: Any) -> Any:
    from dataclasses import replace
    from pathlib import Path
    from .accounts import account_state_path, parse_user_id
    base = Path(session_base).resolve()
    session_name = base.name
    uid = parse_user_id(session_name)
    if uid is None:
        raise SystemExit("account session name is invalid")
    root = base.parent.parent
    state = account_state_path(root, uid)
    return replace(config, session_name=session_name, session_dir=root, state_path=state)


def _ensure_types() -> None:
    from .typesafe import TypeCheckError, check_kernel

    try:
        check_kernel()
    except TypeCheckError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit("pyright strict failed")


def main() -> None:
    from .state import apply_vault_key_env

    apply_vault_key_env()
    parser = argparse.ArgumentParser(prog="hotaru")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--account", type=int, default=None)
    args = parser.parse_args()
    if args.account is not None and not args.check:
        import os as _os
        if _os.environ.get("HOTARU_ACCOUNT_SESSION"):
            from .config import RuntimeConfig
            from .runtime import Runtime
            config = RuntimeConfig.from_database()
            config = _apply_account_session(config, _os.environ["HOTARU_ACCOUNT_SESSION"])
            _ensure_types()
            asyncio.run(Runtime(config).run())
            return
    ensure_kernel_dependencies()
    _ensure_types()
    from .config import RuntimeConfig
    from .runtime import Runtime
    config = RuntimeConfig.from_database()
    if args.check:
        config.validate()
        print("configuration: valid")
        return
    asyncio.run(Runtime(config).run())


if __name__ == "__main__":
    main()
