from __future__ import annotations

import asyncio
import platform
import re
from pathlib import Path
from typing import Any

from goygram import Session

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,60}$")
PENDING_VAULT_NAME = "hotaru-bot-login.vault"


class BotLoginError(RuntimeError):
    pass


def token_problem(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return "bot token is empty"
    if not TOKEN_RE.match(value):
        return "bot token looks like 123456789:AbCdEfGhIjKlMnOpQrStUvWxYz"
    return None


class EchoBot:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        token: str,
        session_dir: Path,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.token = token.strip()
        self.session_dir = Path(session_dir)
        self.owner: int | None = None
        self.app: Any = None
        self.bot_id: int | None = None
        self._event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._ready = asyncio.Event()
        self._pending: asyncio.Queue[str] | None = None

    def _vault(self) -> Path:
        return self.session_dir / PENDING_VAULT_NAME

    def _build_app(self) -> Any:
        from goygram import GoyGram
        from hotaru.update import current_commit

        vault = self._vault()
        vault.parent.mkdir(parents=True, exist_ok=True)
        session = Session(name=vault.with_suffix("").name, path=vault)
        app = GoyGram(
            device_model="Hotaru",
            system_version=f"{platform.system()} {platform.release()}",
            app_version=f"Hotaru {current_commit()}",
            bot_token=self.token,
            api_id=self.api_id,
            api_hash=self.api_hash,
            session_name=session.name,
            session=session,
            intake="mtproto",
            default_transport="mtproto",
        )
        app.on_msg(self._on_message)
        return app

    async def _on_message(self, message: Any) -> None:
        sender = getattr(message, "from_id", None)
        chat = getattr(message, "chat_id", None)
        if not isinstance(sender, int) or sender <= 0:
            return
        text = str(getattr(message, "text", "") or "")
        pending = self._pending
        if pending is not None:
            await pending.put(text)
            return
        if self.owner is not None:
            return
        if not isinstance(chat, int) or chat <= 0:
            chat = sender
        self.owner = sender
        try:
            await self.app.send_msg(chat, str(sender))
        except Exception:
            pass
        self._event.set()

    async def send(self, text: str) -> None:
        if self.owner is None or self.app is None:
            raise BotLoginError("the bot has no owner chat yet")
        await self.app.send_msg(self.owner, text)

    async def ask(self, prompt: str, *, timeout: float = 300.0) -> str:
        if self.owner is None:
            raise BotLoginError("the bot has no owner chat yet")
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending = queue
        try:
            await self.send(prompt)
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BotLoginError(f"no answer in the bot chat: {prompt}") from exc
        finally:
            self._pending = None

    async def _authenticate(self) -> None:
        from goygram.security import bootstrap_session

        result = await bootstrap_session(
            self.app.core,
            api_id=self.api_id,
            api_hash=self.api_hash,
            session_name=self.app.core.session_name,
            bot_token=self.token,
            session=self.app.session,
        )
        if not result or self.app.session.auth_key is None or not self.app.session.is_bot:
            raise BotLoginError("bot authorization did not complete")

    async def _poll(self) -> None:
        await self.app.core.fsm.start()
        dispatch = asyncio.create_task(self.app.core.disp.consume(), name="hotaru:botlogin-dispatch")
        await self.app.mt.start()
        reader = self.app.mt.reader_task
        if reader is None:
            raise BotLoginError("bot reader did not start")
        tasks: list[asyncio.Task[Any]] = [dispatch, reader]
        self._tasks = tasks
        self._ready.set()
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled():
                task.result()

    async def start(self) -> None:
        self.app = self._build_app()
        await self._authenticate()
        self.bot_id = self.app.session.self_id
        self._tasks = []
        self._runner = asyncio.create_task(self._poll(), name="hotaru:botlogin-poll")
        await asyncio.wait_for(self._ready.wait(), timeout=60.0)

    async def wait_for_owner(self, timeout: float) -> int:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BotLoginError("the bot did not receive a message in time") from exc
        if self.owner is None:
            raise BotLoginError("the bot did not report a Telegram ID")
        return self.owner

    def move_vault(self, user_id: int) -> Path:
        from hotaru.accounts import bot_vault_path

        source = self._vault()
        target = bot_vault_path(self.session_dir, user_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not target.exists():
            source.replace(target)
        return target

    async def close(self) -> None:
        runner = getattr(self, "_runner", None)
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.app is not None:
            try:
                await self.app.close()
            except Exception:
                pass
            self.app = None


async def bot_owner_id(
    api_id: int,
    api_hash: str,
    token: str,
    session_dir: Path,
    timeout: float = 300.0,
) -> tuple[EchoBot, int]:
    bot = EchoBot(api_id, api_hash, token, session_dir)
    await bot.start()
    try:
        owner = await bot.wait_for_owner(timeout)
    except BaseException:
        await bot.close()
        raise
    return bot, owner
