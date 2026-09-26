from __future__ import annotations

from typing import Any
import argparse
import asyncio
import os
import signal
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
    if root.name == "sanctuary":
        root = root.parent
    state = account_state_path(root, uid)
    return replace(config, session_name=session_name, session_dir=root, state_path=state, owner_id=uid, bot_token=None)


async def _run(runtime: Any) -> None:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await runtime.run()
    finally:
        try:
            await runtime.close()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    from .state import apply_vault_key_env

    apply_vault_key_env()
    parser = argparse.ArgumentParser(prog="hotaru")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--account", type=int, default=None)
    args = parser.parse_args()
    if not os.environ.get("HOTARU_ACCOUNT_LOCK"):
        ensure_kernel_dependencies()
    from .config import RuntimeConfig
    from .runtime import Runtime
    path = os.environ.get("HOTARU_ACCOUNT_STATE") if args.account is not None else None
    config = RuntimeConfig.from_database(path, discover=False) if path else RuntimeConfig.from_database()
    if args.account is not None:
        from pathlib import Path
        from .accounts import AccountManager, account_config
        from .state import StateStore
        root = Path(os.environ.get("HOTARU_ACCOUNT_ROOT", str(config.session_dir))).resolve()
        state = StateStore(root / "sanctuary/state.sqlite3")
        try:
            manager = AccountManager(state, root)
            profile = manager.profile(args.account)
            if profile is None:
                raise SystemExit(f"Account #{args.account} not found.")
            if not profile.enabled:
                raise SystemExit(f"Account #{args.account} is disabled.")
            config = account_config(_apply_account_session(config, profile.session_base), profile)
            os.environ['HOTARU_ACCOUNT_SESSION'] = profile.session_base
        finally:
            state.close()
    if args.check:
        config.validate()
        print("configuration: valid")
        return
    try:
        asyncio.run(_run(Runtime(config)))
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
