import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .capabilities import CapabilityBroker
from .backup import BackupService
from .callbacks import CallbackBinding, CallbackDenied, CallbackRouter
from .config import RuntimeConfig
from .commands import CommandParser
from goygram.types import InlineObj
from .events import EventRouter
from relay.inline import InlineManager
from relay.sandbox import ModuleSandbox
from relay.caps import CapabilityHost, describe as describe_caps
from relay.firewall import install as install_firewall
from .kernel import Kernel
from .modules import ModuleStager
from .activation import ModuleManager
from .observatory import Observatory
from .registry import Handler
from .response import ModuleContextFactory, ResponseService
from .security import SecurityGate
from .state import StateStore
from .supervisor import ConnectionSupervisor, Health
from .tasks import TaskSupervisor


@dataclass(slots=True)
class Runtime:
    KERNEL_MODULE_ID = "kernel-core"
    config: RuntimeConfig
    app: Any = None
    kernel: Kernel | None = None
    state: StateStore | None = None
    capabilities: CapabilityBroker | None = None
    modules: ModuleManager | None = None
    stager: ModuleStager | None = None
    responses: ResponseService | None = None
    callbacks: CallbackRouter | None = None
    observatory: Observatory | None = None
    backups: BackupService | None = None
    context_factory: ModuleContextFactory | None = None
    tasks: TaskSupervisor | None = None
    event_router: EventRouter | None = None
    inline: InlineManager | None = None
    supervisor: ConnectionSupervisor | None = None
    security: SecurityGate | None = None
    sandbox: ModuleSandbox | None = None
    cap_host: CapabilityHost | None = None
    closed: bool = False
    _inline_forms: dict[str, tuple[str, list[dict[str, str]]]] | None = None

    @classmethod
    def from_database(cls, path: str | Path | None = None) -> "Runtime":
        from .config import DEFAULT_STATE_PATH

        return cls(RuntimeConfig.from_database(path or DEFAULT_STATE_PATH))

    def build(self) -> Any:
        self.config.validate()
        if self.app is not None:
            return self.app
        from goygram import GoyGram

        self.config.session_dir.mkdir(parents=True, exist_ok=True)
        install_firewall(self.config.session_dir, self.config.session_dir / f"{self.config.session_name}.vault", self.config.session_dir / f"{self.config.session_name}.session")
        session_name = str(self.config.session_dir / self.config.session_name)
        previous_disable = logging.root.manager.disable
        logging.disable(logging.INFO)
        try:
            self.app = GoyGram(
                bot_token=self.config.bot_token,
                api_id=self.config.api_id,
                api_hash=self.config.api_hash,
                session_name=session_name,
            )
        finally:
            logging.disable(previous_disable)
        logging.getLogger("goygram").setLevel(logging.ERROR)
        for name, logger in logging.Logger.manager.loggerDict.items():
            if name.startswith("goygram") and isinstance(logger, logging.Logger):
                logger.setLevel(logging.ERROR)
        self.responses = ResponseService()
        self.supervisor = ConnectionSupervisor()
        self.security = SecurityGate(self.config.owner_id)
        self.kernel = Kernel(
            parser=CommandParser(self.config.prefix),
            owner_id=self.config.owner_id,
            response_service=self.responses,
            form_sender=self._send_form,
            command_timeout=self.config.command_timeout,
        )
        self.kernel.security = self.security
        self.state = StateStore(self.config.state_path)
        self.capabilities = CapabilityBroker()
        self.context_factory = ModuleContextFactory(self.state, self.responses)
        self.callbacks = CallbackRouter()
        self.cap_host = CapabilityHost(self)
        self.context_factory.cap_host = self.cap_host
        self.context_factory.callback_router = self.callbacks
        self.callbacks.register("caps_confirm", self._caps_confirm)
        self.event_router = EventRouter(self._event_error)
        self.backups = BackupService()
        self.tasks = TaskSupervisor()
        self.modules = ModuleManager(tasks=self.tasks)
        self.stager = ModuleStager(self.modules.loader)
        self.inline = InlineManager(self)
        self._inline_forms = {}
        self.context_factory.inline_manager = self.inline
        self.sandbox = ModuleSandbox(self)
        self.kernel.sandbox = self.sandbox
        self.kernel.context_factory = self.context_factory
        self.kernel.registry.register("ver", self._command_ver, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("st", self._command_st, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("ls", self._command_ls, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("mi", self._command_mi, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("hlp", self._command_hlp, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("ld", self._command_ld, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("ul", self._command_ul, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("rl", self._command_rl, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("rm", self._command_rm, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("bk", self._command_bk, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("bot", self._command_bot, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.kernel.registry.register("trust", self._command_trust, kernel=True, module_id=self.KERNEL_MODULE_ID)
        self.inline.on_inline(self._on_inline_query)
        self.inline.on_callback(self._on_inline_callback)
        self.callbacks.register("help_page", self._help_page)
        self.callbacks.register("module_detail", self._module_detail)
        self.kernel.attach(self.app)
        self.app.on_cb(self._on_callback)
        self.event_router.attach_aux(self.app)
        return self.app

    async def _on_callback(self, callback: Any) -> object | None:
        if self.callbacks is None:
            return None
        if self.security is not None:
            from .security import AccessVerdict

            verdict = self.security.check_callback(callback, transport="mt")
            if verdict is not AccessVerdict.ALLOW:
                return None
        try:
            return await self.callbacks.dispatch(callback)
        except CallbackDenied:
            return None

    async def _send_form(self, command: Any, text: str, buttons: list[dict[str, str]], options: dict[str, Any] | None = None) -> Any:
        if self.inline is None:
            raise RuntimeError("inline bot form transport is unavailable")
        if self.inline.info is None:
            await self.inline.ensure_bot()
        if self.inline.bot_app is None:
            await self.inline.start()
        if self.inline.info is None or self.inline.bot_app is None:
            raise RuntimeError("inline bot form transport is not ready")
        owner = self.kernel.owner_id if self.kernel is not None else None
        if owner is None:
            raise RuntimeError("form owner is missing")
        options = options or {}
        chat_id = getattr(command, "chat_id", None)
        if not isinstance(chat_id, (int, str)):
            raise RuntimeError("form target chat is missing")
        if getattr(command, "src", None) != "bot":
            return await self._insert_inline_form(command, text, buttons, options)
        reply_to = getattr(command, "id", None) or getattr(command, "message_id", None)
        topic_id = options.get("topic_id")
        if not isinstance(topic_id, int) and hasattr(command, "get"):
            for key in ("message_thread_id", "topic_id", "topic"):
                value = command.get(key)
                if isinstance(value, int):
                    topic_id = value
                    break
        sent = await self.inline.bot_app.send_msg(chat_id, text, reply_to=reply_to if isinstance(reply_to, int) else None, topic_id=topic_id if isinstance(topic_id, int) else None, parse_mode="HTML")
        body = sent.get("result", sent) if isinstance(sent, dict) else sent
        form_id = body.get("message_id") if isinstance(body, dict) else getattr(body, "message_id", None)
        if not isinstance(form_id, int):
            raise RuntimeError("inline form message id is missing")
        rebound = []
        for button in buttons:
            handle = button.get("callback_data")
            if isinstance(handle, str):
                rebound.append({"text": button.get("text", ""), "callback_data": self.callbacks.store.rebind(handle, CallbackBinding(owner, chat_id, form_id))})
        await self.inline.bot_app.bot_req("editMessageText", chat_id=chat_id, message_id=form_id, text=text, parse_mode="HTML", reply_markup={"inline_keyboard": [rebound]})
        if hasattr(command, "delete"):
            await command.delete()
        return sent

    async def _insert_inline_form(self, command: Any, text: str, buttons: list[dict[str, str]], options: dict[str, Any] | None = None) -> Any:
        if self.inline is None or self.inline.info is None or self.app is None or self.app.mt is None:
            raise RuntimeError("inline insertion transport is not ready")
        chat_id = getattr(command, "chat_id", None)
        message_id = getattr(command, "id", None) or getattr(command, "message_id", None)
        if not isinstance(message_id, int):
            raise RuntimeError("inline insertion source message is missing")
        if not isinstance(options, dict):
            options = {}
        if not isinstance(options.get("topic_id"), int) and hasattr(command, "get"):
            for key in ("message_thread_id", "topic_id", "topic"):
                value = command.get(key)
                if isinstance(value, int):
                    options["topic_id"] = value
                    break
        nonce = secrets.token_urlsafe(12)
        if self._inline_forms is None:
            self._inline_forms = {}
        inline_buttons = []
        for button in buttons:
            handle = button.get("callback_data")
            if isinstance(handle, str):
                inline_buttons.append({"text": button.get("text", ""), "callback_data": self.callbacks.store.rebind(handle, CallbackBinding(self.kernel.owner_id, None, 0))})
            elif isinstance(button.get("url"), str):
                inline_buttons.append({"text": button.get("text", ""), "url": button["url"]})
        self._inline_forms[nonce] = (text, inline_buttons)
        bot = await self.app.mt.resolve_peer("@" + self.inline.info.username)
        peer = await self.app.mt.resolve_peer(chat_id)
        result = await self.app.mt_req(
            "messages.getInlineBotResults",
            bot=bot,
            peer=peer,
            query="hotaru-form:" + nonce,
            offset="",
        )
        body = result.get("result", result) if isinstance(result, dict) else result
        if isinstance(body, dict) and isinstance(body.get("bot_results"), dict):
            body = body["bot_results"]
        if isinstance(body, dict) and isinstance(body.get("results"), dict):
            body = body["results"]
        if isinstance(body, dict) and isinstance(body.get("query"), dict):
            body = body["query"]
        query_id = body.get("query_id") if isinstance(body, dict) else None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(query_id, (int, str)) or not isinstance(results, list) or not results:
            raise RuntimeError("inline bot returned no form result")
        await self.app.mt_req(
            "messages.sendInlineBotResult",
            peer=peer,
            reply_to={"_": "inputReplyToMessage", "reply_to_msg_id": message_id, **({"top_msg_id": options.get("topic_id")} if isinstance(options, dict) and isinstance(options.get("topic_id"), int) else {})},
            random_id=secrets.randbits(63),
            query_id=query_id,
            id=results[0].get("id"),
            clear_draft=True,
        )
        await self.app.mt_req("messages.deleteMessages", id=[message_id], revoke=True)
        return result

    async def _on_inline_query(self, query: Any) -> None:
        if self.security is not None:
            from .security import AccessVerdict

            verdict = self.security.check(query, transport="inline")
            if verdict is not AccessVerdict.ALLOW:
                return
        text = (query.query or "").strip()
        if text.startswith("hotaru-form:"):
            nonce = text.split(":", 1)[1]
            form = self._inline_forms.get(nonce) if self._inline_forms is not None else None
            if form is None:
                await query.answer([], cache_time=0, is_personal=True)
                return
            form_text, buttons = form
            result = InlineObj.article("hotaru-form", "Hotaru form", form_text, parse_mode="HTML")
            result["reply_markup"] = {"inline_keyboard": [buttons]}
            await query.answer([result], cache_time=0, is_personal=True)
            return
        from . import __version__

        results = []
        lowered = text.casefold()
        if not lowered or "ping" in lowered:
            results.append(InlineObj.article("hotaru-ping", "Ping", "pong!", description="Measure roundtrip"))
        if not lowered or "stat" in lowered or "st" in lowered:
            body = self._command_st(SimpleNamespace(args=()))
            results.append(InlineObj.article("hotaru-st", "Status", body, description="Runtime status"))
        if not lowered or "help" in lowered or "hlp" in lowered:
            results.append(InlineObj.article("hotaru-hlp", "Help", "Send !hlp to list modules", description="Command catalog"))
        if not lowered or "ver" in lowered:
            results.append(InlineObj.article("hotaru-ver", "Version", f"Hotaru {__version__}", description="Kernel version"))
        await query.answer(results, cache_time=0, is_personal=True)

    async def _render_inline_form(self, query: Any) -> None:
        text = str(query.query or "")
        if not text.startswith("hotaru-form:") or self._inline_forms is None:
            return
        form = self._inline_forms.get(text.split(":", 1)[1])
        if form is None:
            await query.answer([], cache_time=0, is_personal=True)
            return
        body, buttons = form
        await query.answer([InlineObj.article("hotaru-form", "Hotaru form", body, kbd={"inline_keyboard": [buttons]})], cache_time=0, is_personal=True)

    async def _on_inline_callback(self, callback: Any) -> None:
        if self.security is None or self.callbacks is None:
            return None
        from .security import AccessVerdict

        verdict = self.security.check_callback(callback, transport="inline")
        if verdict is not AccessVerdict.ALLOW:
            try:
                await callback.answer()
            except Exception:
                pass
            return None
        try:
            return await self.callbacks.dispatch(callback)
        except Exception as exc:
            if self.observatory is not None:
                self.observatory.emit("inline", "callback_error", error=type(exc).__name__, detail=str(exc)[:240])
            return None

    def _command_ver(self, invocation: Any) -> str:
        from . import __version__

        return f"Hotaru {__version__}"

    def _command_st(self, invocation: Any) -> str:
        status = self.status()
        flags = ", ".join(f"{key}={value}" for key, value in status.items())
        lines = [f"health: {self.health()}", flags]
        if self.supervisor is not None:
            state = self.supervisor.state
            lines.append(f"connection: {state.health.value} reconnects={state.reconnects} mt={state.mt_ready} bot={state.bot_ready}")
            if state.last_error:
                lines.append(f"last_error: {state.last_error}")
        if self.kernel is not None:
            lines.append(f"running_commands: {self.kernel.running()}")
        if self.inline is not None and self.inline.info is not None:
            running = self.inline._task is not None and not self.inline._task.done()
            lines.append(f"inline: @{self.inline.info.username} ({'running' if running else 'stopped'})")
        return "\n".join(lines)

    def _command_ls(self, invocation: Any) -> str:
        if self.modules is None:
            return "modules: unavailable"
        items = self.modules.items()
        if not items:
            return "modules: none"
        return "modules:\n" + "\n".join(sorted(item.loaded.manifest.module_id for item in items))

    def _module_detail_text(self, module_id: str) -> str:
        if self.modules is None:
            return "module unavailable"
        active = self.modules.get(module_id)
        if active is None:
            if self.state is not None and module_id in self.state.module_ids():
                return f"module: {module_id}\nstatus: disabled\nlasterror: {self.state.namespace(module_id).get('lasterror', 'none')}"
            return f"module not active: {module_id}"
        manifest = active.loaded.manifest
        commands = ", ".join(manifest.commands) if manifest.commands else "none"
        capabilities = ", ".join(manifest.capabilities) if manifest.capabilities else "none"
        return f"module: {manifest.module_id}\nversion: {manifest.version}\ncommands: {commands}\ncapabilities: {capabilities}\ndescription: {manifest.description}"

    def _command_mi(self, invocation: Any) -> str:
        if len(invocation.args) != 1:
            return "usage: !mi <module-id>"
        return self._module_detail_text(invocation.args[0].casefold())

    async def _command_hlp(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        page = 0
        if invocation.args:
            if len(invocation.args) != 1:
                return "usage: !hlp [page] | !hlp <module-id>"
            if not invocation.args[0].isdigit():
                return self._command_mi(invocation)
            page = int(invocation.args[0])
        return self._help_render(page, invocation.chat_id, invocation.message_id)

    def _help_render(self, page: int, chat_id: int | str | None, message_id: int) -> tuple[str, list[dict[str, str]]] | str:
        if self.kernel is None or self.callbacks is None or page < 0:
            return "help unavailable"
        names = sorted(self.kernel.registry.names())
        entries: list[str] = []
        entry_ids: list[str | None] = []
        if self.modules is not None:
            active = {item.loaded.manifest.module_id: item.loaded.manifest.version for item in self.modules.items()}
            known = set(self.state.module_ids()) if self.state is not None else set()
            for module_id in sorted(set(active) | known):
                if module_id in active:
                    entries.append(f"{module_id} [loaded] v{active[module_id]}")
                else:
                    entries.append(f"{module_id} [untrusted]")
                entry_ids.append(module_id)
        if not entries:
            entries = [f"{self.config.prefix}{name}" for name in names]
            entry_ids = [None] * len(entries)
        page_size = 24
        start = page * page_size
        if start >= len(entries) and entries:
            return "help page unavailable"
        current = entries[start : start + page_size]
        text = "catalog: " + (", ".join(current) or "none")
        if self.kernel.owner_id is None or chat_id is None:
            return text
        buttons: list[dict[str, str]] = []
        for module_id in entry_ids[start : start + page_size]:
            if module_id is None:
                continue
            handle = self.callbacks.store.issue(
                CallbackBinding(self.kernel.owner_id, chat_id, message_id),
                {"action": "module_detail", "payload": module_id},
            )
            buttons.append({"text": module_id, "callback_data": handle})
        if start + page_size < len(entries):
            handle = self.callbacks.store.issue(
                CallbackBinding(self.kernel.owner_id, chat_id, message_id),
                {"action": "help_page", "payload": page + 1},
            )
            buttons.append({"text": "Next", "callback_data": handle})
        return text, buttons

    async def _command_bot(self, invocation: Any) -> str:
        if self.inline is None:
            return "inline bot: unavailable"
        info = self.inline.info
        if info is None:
            try:
                info = await self.inline.ensure_bot()
            except Exception as exc:
                return f"inline bot provisioning failed: {type(exc).__name__}"
        if self.inline._task is None or self.inline._task.done():
            await self.inline.start()
        running = self.inline._task is not None and not self.inline._task.done()
        return f"inline bot: @{info.username} ({'running' if running else 'starting'})"

    def stage_module_url(self, source: str, destination: str | Path | None = None) -> Any:
        if self.stager is None:
            raise RuntimeError("build the runtime before staging modules")
        target = Path(destination) if destination is not None else self.config.state_path.parent / "constellations"
        return self.stager.stage_url(source, target)

    async def _download_module_message(self, message: Any, destination: Path) -> None:
        source = message
        if not (source.get("document") or source.get("media")):
            candidate = message.get("reply_to_message") or message.get("reply")
            source = candidate if candidate is not None and (candidate.get("document") or candidate.get("media")) else None
        if source is None or not hasattr(source, "get"):
            reply_to = message.get("reply_to")
            reply_id = reply_to.get("reply_to_msg_id") if isinstance(reply_to, dict) else None
            if isinstance(reply_id, int) and getattr(message, "src", None) != "bot":
                peer = await message.app.mt.resolve_peer(message.chat_id)
                result = await message.app.mt_req(
                    "messages.getHistory",
                    peer=peer,
                    offset_id=reply_id + 1,
                    offset_date=0,
                    add_offset=-1,
                    limit=3,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
                body = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
                messages = body.get("messages") if isinstance(body, dict) else None
                source = next((item for item in messages or [] if item.get("id") == reply_id), None)
        if source is None or not hasattr(source, "get"):
            raise ValueError("module file is missing")
        document = source.get("document")
        media = source.get("media")
        if document is None and isinstance(media, dict):
            document = media.get("document")
        if not isinstance(document, dict):
            raise ValueError("module file must be a document")
        if getattr(source, "src", getattr(message, "src", None)) == "bot":
            if hasattr(source, "download"):
                await source.download(str(destination))
            else:
                file_id = document.get("file_id")
                if not isinstance(file_id, str):
                    raise ValueError("Bot API document file_id is missing")
                await message.app.download_file(file_id, str(destination))
            return
        required = ("id", "access_hash")
        if any(not isinstance(document.get(key), int) for key in required):
            raise ValueError("MTProto document location is incomplete")
        file_reference = document.get("file_reference", b"")
        if isinstance(file_reference, str):
            try:
                file_reference = bytes.fromhex(file_reference)
            except ValueError:
                file_reference = file_reference.encode("utf-8")
        if not isinstance(file_reference, (bytes, bytearray)):
            raise ValueError("MTProto file reference is invalid")
        location = {
            "_": "inputDocumentFileLocation",
            "id": document["id"],
            "access_hash": document["access_hash"],
            "file_reference": bytes(file_reference),
            "thumb_size": "",
        }
        await message.app.mt.download_file(location, str(destination), limit=524288)

    async def _command_ld(self, invocation: Any) -> str:
        if len(invocation.args) > 1:
            return "usage: .ld <raw-url> | reply to a .hmod file | .hmod caption"
        temporary: Path | None = None
        try:
            if invocation.args:
                source = invocation.args[0]
                if source.startswith("https://"):
                    loaded = self.stage_module_url(source)
                else:
                    loaded = self.stage_module(source)
            else:
                if invocation.message is None:
                    return "usage: .ld <raw-url> | reply to a .hmod file | .hmod caption"
                fd, raw_path = tempfile.mkstemp(prefix=".hotaru-download-", suffix=".hmod")
                os.close(fd)
                temporary = Path(raw_path)
                await self._download_module_message(invocation.message, temporary)
                loaded = self.stage_module(temporary)
        except Exception as exc:
            if self.observatory is not None:
                self.observatory.emit("modules", "load_error", error=type(exc).__name__, detail=str(exc)[:240])
            return f"load failed: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        fingerprint = self._caps_fingerprint(loaded.manifest)
        if self._caps_consented(loaded.manifest.module_id, fingerprint):
            if self.modules is not None and self.modules.get(loaded.manifest.module_id) is not None:
                result = await self._command_rl(SimpleNamespace(args=(loaded.manifest.module_id,)))
                if result.startswith("reloaded:"):
                    return f"updated: {loaded.manifest.module_id} {loaded.manifest.version}"
                return result
            try:
                await self.activate_module(str(loaded.path))
            except Exception as exc:
                return f"staged (activation failed): {type(exc).__name__}"
            return f"active: {loaded.manifest.module_id} {loaded.manifest.version}"
        screen = await self._render_caps_screen(
            loaded.manifest.module_id,
            loaded.manifest,
            invocation.chat_id,
            invocation.message_id,
        )
        if isinstance(screen, tuple):
            return f"staged: {loaded.manifest.module_id} {loaded.manifest.version}\n" + screen[0], screen[1]
        return f"staged (untrusted): {loaded.manifest.module_id} {loaded.manifest.version}\n{screen}"

    async def _command_ul(self, invocation: Any) -> str:
        if len(invocation.args) != 1 or self.modules is None or self.state is None:
            return "usage: .ul <module-id>"
        module_id = invocation.args[0].casefold()
        active = self.modules.get(module_id)
        namespace = self.state.namespace(module_id)
        source_path = active.loaded.path if active is not None else Path(namespace.get("sourcepath", self.config.state_path.parent / "constellations" / f"{module_id}.hmod"))
        if not source_path.is_file():
            return f"module not found: {module_id}"
        try:
            self._backup_before_activation(source_path)
            if active is not None:
                await self.deactivate_module(module_id)
            source_path.unlink()
        except Exception as exc:
            if active is not None and self.modules.get(module_id) is None and source_path.is_file():
                try:
                    await self.activate_module(str(source_path))
                except Exception:
                    pass
            return f"unload failed: {type(exc).__name__}"
        self.state.delete_module(module_id)
        return f"unloaded permanently: {module_id}"

    @staticmethod
    def _version_key(value: str) -> tuple[int, ...] | None:
        parts = value.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            return None
        return tuple(int(part) for part in parts)

    async def _command_rl(self, invocation: Any) -> str:
        if len(invocation.args) not in (1, 2) or self.modules is None:
            return "usage: !rl <module-id> [force]"
        force = len(invocation.args) == 2 and invocation.args[1].casefold() == "force"
        if len(invocation.args) == 2 and not force:
            return "usage: !rl <module-id> [force]"
        module_id = invocation.args[0].casefold()
        active = self.modules.get(module_id)
        if active is None:
            return f"module not active: {module_id}"
        old_path = active.loaded.path
        old_source = active.loaded.source
        candidate = self.config.state_path.parent / "constellations" / f"{module_id}.hmod"
        reload_path = candidate if candidate.is_file() else old_path
        if reload_path == candidate:
            candidate_loaded = self.modules.loader.load(candidate)
            if candidate_loaded.manifest.module_id != module_id:
                return f"module id mismatch: {module_id}"
            current_version = self._version_key(active.loaded.manifest.version)
            candidate_version = self._version_key(candidate_loaded.manifest.version)
            if not force and current_version is not None and candidate_version is not None and candidate_version < current_version:
                if self.observatory is not None:
                    self.observatory.emit("modules", "update_blocked", module=module_id, old_version=active.loaded.manifest.version, new_version=candidate_loaded.manifest.version, reason="downgrade")
                return f"reload blocked: version downgrade {active.loaded.manifest.version} to {candidate_loaded.manifest.version}"
        await self.deactivate_module(module_id)
        try:
            await self.activate_module(str(reload_path))
        except Exception as exc:
            if self.observatory is not None:
                self.observatory.emit("modules", "update_error", module=module_id, error=type(exc).__name__)
            try:
                if self.stager is None:
                    raise RuntimeError("module stager is unavailable")
                self.stager.stage_text(old_source, old_path.parent)
                await self.modules.activate_source(old_path, self.kernel)
            except Exception:
                return f"reload failed: {type(exc).__name__}; rollback failed"
            if self.observatory is not None:
                self.observatory.emit("modules", "rollback_restored", module=module_id, version=active.loaded.manifest.version)
            return f"reload failed: {type(exc).__name__}; previous version restored"
        if self.observatory is not None:
            self.observatory.emit("modules", "update_applied", module=module_id, version=self.modules.get(module_id).loaded.manifest.version)
        return f"reloaded: {module_id}"

    async def _command_trust(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        if len(invocation.args) != 1 or self.modules is None or self.state is None:
            return "usage: !trust <module-id>"
        module_id = invocation.args[0].casefold()
        if self.modules.get(module_id) is not None:
            return f"module already active: {module_id}"
        namespace = self.state.namespace(module_id)
        source_path = namespace.get("sourcepath")
        if not isinstance(source_path, str):
            candidate = self.config.state_path.parent / "constellations" / f"{module_id}.hmod"
            if not candidate.is_file():
                return f"module not found: {module_id}"
            source_path = str(candidate)
        try:
            loaded = self.modules.loader.load(source_path)
        except Exception as exc:
            return f"trust failed: {type(exc).__name__}"
        fingerprint = self._caps_fingerprint(loaded.manifest)
        if not self._caps_consented(module_id, fingerprint):
            return await self._render_caps_screen(module_id, loaded.manifest, invocation.chat_id, invocation.message_id)
        try:
            await self.activate_module(source_path)
        except Exception as exc:
            return f"trust failed: {type(exc).__name__}"
        return f"active: {module_id}"

    def _caps_fingerprint(self, manifest: Any) -> str:
        import hashlib

        payload = json.dumps([manifest.module_id, manifest.version, sorted(manifest.capabilities)], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _caps_consented(self, module_id: str, fingerprint: str) -> bool:
        if self.state is None:
            return False
        namespace = self.state.namespace(module_id)
        return namespace.get("caps-consent") == fingerprint

    def _mark_caps_consent(self, module_id: str, fingerprint: str) -> None:
        if self.state is not None:
            self.state.namespace(module_id).set("caps-consent", fingerprint)

    async def _render_caps_screen(self, module_id: str, manifest: Any, chat_id: int | str | None, message_id: int) -> tuple[str, list[dict[str, str]]] | str:
        if self.callbacks is None or self.kernel is None or self.kernel.owner_id is None or chat_id is None:
            lines = [f"module {module_id} v{manifest.version} requests capabilities:"]
            lines.append(describe_caps(manifest.capabilities) or "none")
            lines.append("re-run !trust from your Saved Messages to confirm")
            return "\n".join(lines)
        text = f"module {module_id} v{manifest.version} requests capabilities:\n" + (describe_caps(manifest.capabilities) or "none") + "\n\nНажми кнопку Confirm ниже, чтобы загрузить модуль."
        handle = self.callbacks.store.issue(
            CallbackBinding(self.kernel.owner_id, chat_id, 0),
            {"action": "caps_confirm", "payload": module_id},
        )
        return (text, [{"text": "Confirm", "callback_data": handle}])

    async def _caps_confirm(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str) or self.modules is None or self.state is None:
            await callback.answer("Invalid request", alert=True)
            return None
        module_id = payload.casefold()
        namespace = self.state.namespace(module_id)
        source_path = namespace.get("sourcepath")
        if not isinstance(source_path, str):
            candidate = self.config.state_path.parent / "constellations" / f"{module_id}.hmod"
            if not candidate.is_file():
                await callback.answer("Source missing", alert=True)
                return await callback.edit(f"module not found: {module_id}")
            source_path = str(candidate)
        try:
            loaded = self.modules.loader.load(source_path)
        except Exception as exc:
            await callback.answer("Load failed", alert=True)
            return await callback.edit(f"trust failed: {type(exc).__name__}")
        try:
            self._mark_caps_consent(module_id, self._caps_fingerprint(loaded.manifest))
            await self.activate_module(source_path)
        except Exception as exc:
            namespace.set("lasterror", f"{type(exc).__name__}: {str(exc)[:240]}")
            if self.observatory is not None:
                self.observatory.emit("modules", "activation_error", module=module_id, error=type(exc).__name__, detail=str(exc)[:240])
            await callback.answer("Activation failed", alert=True)
            return await callback.edit(f"trust failed: {type(exc).__name__}: {str(exc)[:120]}")
        await callback.answer("Module loaded")
        text = f"active: {module_id}"
        if getattr(callback, "inline_message_id", None) and getattr(callback, "app", None) is not None:
            return await callback.app.bot_req("editMessageText", inline_message_id=callback.inline_message_id, text=text)
        return await callback.edit(text)

    def create_backup(self) -> Path:
        if self.backups is None or self.state is None or self.modules is None:
            raise RuntimeError("backup services are not ready")
        module_paths = [active.loaded.path for active in self.modules.items()]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self.config.state_path.parent / "backups" / f"{stamp}.hbk"
        result = self.backups.create(destination, state_path=self.config.state_path, module_paths=module_paths, metadata={"reason": "operator"})
        removed = self.backups.prune(destination.parent, keep=self.config.backup_keep)
        if self.observatory is not None:
            self.observatory.emit("backup", "created", files=len(module_paths) + 1, removed=len(removed))
        return result

    async def _command_bk(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        if not invocation.args:
            try:
                archive = self.create_backup()
            except Exception as exc:
                return f"backup failed: {type(exc).__name__}"
            return f"backup created: {archive.name}"
        action = invocation.args[0].casefold()
        if action == "list" and len(invocation.args) == 1:
            directory = self.config.state_path.parent / "backups"
            names = sorted(path.name for path in directory.glob("*.hbk") if path.is_file())
            return "backups: none" if not names else "backups:\n" + "\n".join(names)
        if action == "test" and len(invocation.args) == 2:
            try:
                plan = self.backups.plan(invocation.args[1])
            except Exception as exc:
                return f"backup invalid: {type(exc).__name__}"
            return f"backup valid: {len(plan.files)} files"
        if action == "restore" and len(invocation.args) == 2 and self.callbacks is not None and self.kernel is not None and self.kernel.owner_id is not None:
            try:
                self.backups.plan(invocation.args[1])
            except Exception as exc:
                return f"backup invalid: {type(exc).__name__}"
            handle = self.callbacks.store.issue(
                CallbackBinding(self.kernel.owner_id, invocation.chat_id, invocation.message_id),
                {"action": "restore_confirm", "payload": invocation.args[1]},
            )
            return ("confirm restore", [{"text": "Confirm", "callback_data": handle}])
        return "usage: !bk | !bk list | !bk test <archive> | !bk restore <archive>"

    async def _command_rm(self, invocation: Any) -> tuple[str, list[dict[str, str]]] | str:
        if len(invocation.args) != 1 or self.modules is None or self.callbacks is None:
            return "usage: !rm <module-id>"
        if self.kernel is None or self.kernel.owner_id is None:
            return "rm unavailable: explicit owner id required"
        module_id = invocation.args[0].casefold()
        if self.modules.get(module_id) is None:
            return f"module not active: {module_id}"
        handle = self.callbacks.store.issue(
            CallbackBinding(self.kernel.owner_id, invocation.chat_id, invocation.message_id),
            {"action": "remove_confirm", "payload": module_id},
        )
        return (f"confirm removal: {module_id}", [{"text": "Confirm", "callback_data": handle}])

    async def _remove_confirm(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str) or self.modules is None:
            await callback.answer("Invalid removal request", alert=True)
            return None
        active = self.modules.get(payload)
        if active is None:
            await callback.answer("Module is already unloaded", alert=True)
            return await callback.edit(f"module not active: {payload}")
        path = active.loaded.path
        self._backup_before_activation(path)
        await self.deactivate_module(payload)
        try:
            path.unlink()
        except OSError:
            await self.modules.activate_source(path, self.kernel)
            await callback.answer("Removal failed", alert=True)
            return await callback.edit(f"removal failed: {payload}")
        await callback.answer("Module removed", alert=True)
        return await callback.edit(f"removed: {payload}")

    async def _module_detail(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str):
            await callback.answer("Invalid module", alert=True)
            return None
        return await callback.edit(self._module_detail_text(payload))

    async def _help_page(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, int) or payload < 0:
            await callback.answer("Invalid help page", alert=True)
            return None
        result = self._help_render(payload, callback.chat_id, callback.msg_id)
        if isinstance(result, tuple):
            text, buttons = result
            return await callback.edit(text, kbd=buttons)
        return await callback.edit(result)

    async def _restore_confirm(self, callback: Any, payload: Any) -> object:
        if not isinstance(payload, str) or self.backups is None:
            await callback.answer("Invalid restore request", alert=True)
            return None
        try:
            plan = self.backups.plan(payload)
            await self.restore_filesystem(plan, self.config.state_path.parent / "constellations")
        except Exception as exc:
            await callback.answer("Restore failed", alert=True)
            return await callback.edit(f"restore failed: {type(exc).__name__}")
        await callback.answer("Restore completed", alert=True)
        return await callback.edit("restore completed")

    def _event_error(self, error: Exception) -> None:
        if self.observatory is not None:
            self.observatory.emit("events", "handler_error", error=type(error).__name__)

    def register_callback(self, action: str, handler: Any) -> None:
        if self.callbacks is None:
            raise RuntimeError("build the runtime before registering callbacks")
        self.callbacks.register(action, handler)

    def register_command(self, name: str, handler: Handler, *, kernel: bool = False) -> None:
        if self.kernel is None:
            raise RuntimeError("build the runtime before registering commands")
        self.kernel.registry.register(name, handler, kernel=kernel)

    def register_module_command(self, module_id: str, name: str, handler: Handler) -> None:
        if self.kernel is None:
            raise RuntimeError("build the runtime before registering commands")
        self.kernel.register_module_command(module_id, name, handler)

    def _backup_before_activation(self, path: str | Path) -> Path:
        if self.backups is None or self.state is None or self.modules is None:
            raise RuntimeError("backup services are not ready")
        candidate = Path(path)
        self.modules.loader.load(candidate)
        module_paths = [active.loaded.path for active in self.modules.items()]
        if candidate not in module_paths:
            module_paths.append(candidate)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self.config.state_path.parent / "backups" / f"{stamp}.hbk"
        result = self.backups.create(
            destination,
            state_path=self.config.state_path,
            module_paths=module_paths,
            metadata={"reason": "module_activation", "module": str(candidate.name)},
        )
        removed = self.backups.prune(destination.parent, keep=self.config.backup_keep)
        if self.observatory is not None:
            self.observatory.emit("backup", "created", files=len(module_paths) + 1, removed=len(removed))
        return result

    def stage_module(self, source: str | Path, destination: str | Path | None = None) -> Any:
        if self.stager is None:
            raise RuntimeError("build the runtime before staging modules")
        target = Path(destination) if destination is not None else self.config.state_path.parent / "constellations"
        return self.stager.stage(source, target)

    async def activate_module(self, path: str, *, health: Any = None) -> Any:
        if self.modules is None or self.kernel is None:
            raise RuntimeError("build the runtime before activating modules")
        self._backup_before_activation(path)
        result = await self.modules.activate_source(path, self.kernel, health=health, sandbox=self.sandbox)
        module_id = result.loaded.manifest.module_id
        namespace = self.state.namespace(module_id) if self.state is not None else None
        if namespace is not None:
            namespace.set("sourcepath", str(result.loaded.path))
            namespace.set("moduleversion", result.loaded.manifest.version)
            namespace.delete("lasterror")
        return result

    async def deactivate_module(self, module_id: str) -> bool:
        if self.modules is None:
            raise RuntimeError("build the runtime before deactivating modules")
        if self.sandbox is not None:
            self.sandbox.stop_module(module_id)
        if self.callbacks is not None:
            self.callbacks.unregister_module(module_id)
        return await self.modules.deactivate(module_id)

    async def restore_backup(self, plan: Any, activate: Any, *, rollback: Any | None = None, timeout: float = 10.0) -> Any:
        if self.backups is None:
            raise RuntimeError("backup service is not ready")
        return await self.backups.restore(plan, activate, rollback=rollback, timeout=timeout)

    async def restore_filesystem(self, plan: Any, modules_path: str | Path) -> None:
        if self.backups is None or self.state is None or self.modules is None:
            raise RuntimeError("runtime services are not ready")
        if self.modules.items():
            raise RuntimeError("unload modules before filesystem restore")
        state = self.state
        self.state = None
        state.close()
        try:
            await self.backups.restore(
                plan,
                lambda staged: self.backups.activate_staged(
                    staged,
                    state_path=self.config.state_path,
                    modules_path=modules_path,
                ),
            )
        finally:
            self.state = StateStore(self.config.state_path)
            self.context_factory = ModuleContextFactory(self.state, self.responses)
            self.context_factory.cap_host = self.cap_host
            self.context_factory.callback_router = self.callbacks
            self.context_factory.inline_manager = self.inline
            if self.kernel is not None:
                self.kernel.context_factory = self.context_factory
            self.modules = ModuleManager(tasks=self.tasks)

    def status(self) -> dict[str, Any]:
        return {
            "runtime": "closed" if self.closed else ("ready" if self.app is not None else "new"),
            "kernel": self.kernel is not None,
            "state": self.state is not None,
            "capabilities": self.capabilities is not None,
            "modules": self.modules is not None,
            "responses": self.responses is not None,
            "callbacks": self.callbacks is not None,
            "observatory": self.observatory is not None,
            "backups": self.backups is not None,
            "tasks": self.tasks is not None,
            "event_router": self.event_router is not None,
        }

    def health(self) -> bool:
        status = self.status()
        return status["runtime"] == "ready" and all(value for key, value in status.items() if key != "runtime")

    async def restore_enabled_modules(self, *, timeout: float = 60.0) -> tuple[str, ...]:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.state is None or self.modules is None or self.kernel is None:
            raise RuntimeError("runtime services are not ready")
        restored: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                for module_id in self.state.module_ids()[:256]:
                    namespace = self.state.namespace(module_id)
                    if self.modules.get(module_id) is not None:
                        continue
                    consent = namespace.get("caps-consent")
                    if not isinstance(consent, str):
                        continue
                    source_path = namespace.get("sourcepath")
                    if not isinstance(source_path, str):
                        continue
                    try:
                        loaded = self.modules.loader.load(source_path)
                        if loaded.manifest.module_id != module_id:
                            raise ValueError("module id mismatch")
                        if self._caps_fingerprint(loaded.manifest) != consent:
                            raise ValueError("capabilities changed; !trust required")
                        await self.activate_module(source_path)
                    except Exception as exc:
                        namespace.set("lasterror", f"{type(exc).__name__}: {str(exc)[:240]}")
                        if self.observatory is not None:
                            self.observatory.emit("modules", "restore_error", module=module_id, error=type(exc).__name__, detail=str(exc)[:240])
                        continue
                    restored.append(module_id)
        except TimeoutError:
            if self.observatory is not None:
                self.observatory.emit("modules", "restore_timeout", restored=len(restored))
        return tuple(restored)

    async def run(self) -> None:
        if self.app is None:
            self.build()
        assert self.app is not None
        await self.restore_enabled_modules()
        if self.inline is not None:
            try:
                await self.inline.start()
                if self.observatory is not None and self.inline.info is not None:
                    self.observatory.emit("inline", "started", username=self.inline.info.username)
            except Exception as exc:
                if self.observatory is not None:
                    self.observatory.emit("inline", "start_failed", error=type(exc).__name__)
        if self.supervisor is not None:
            self.supervisor.mark_ready(mt=self.app.mt is not None, bot=self.app.bot is not None)
        try:
            await self.app.run()
        finally:
            if self.supervisor is not None:
                self.supervisor.mark_stopped()

    def stop(self) -> None:
        if self.app is not None:
            self.app.stop()

    async def close(self) -> None:
        if self.closed:
            return
        if self.inline is not None:
            await self.inline.stop()
        if self.kernel is not None:
            await self.kernel.cancel_all()
        if self.modules is not None:
            for active in tuple(self.modules.items()):
                await self.modules.deactivate(active.loaded.manifest.module_id)
        if self.tasks is not None:
            await self.tasks.close()
        if self.sandbox is not None:
            self.sandbox.stop_all()
        if self.app is not None:
            await self.app.close()
        if self.state is not None:
            self.state.close()
        self.closed = True
