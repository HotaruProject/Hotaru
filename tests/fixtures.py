import base64
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from goygram.types.msg import MsgObj
from hotaru.config import RuntimeConfig
from hotaru.runtime import Runtime


class Offline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="hotaru-offline-")))
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.enterContext(patch("goygram.client.AppCore._init_tl_schema"))
        self.enterContext(patch("goygram.client.AppCore._load_vault_from_disk"))
        self.enterContext(patch("hotaru.runtime.install_firewall"))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network disabled")))
        self.enterContext(patch("socket.socket.connect_ex", side_effect=AssertionError("network disabled")))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("network disabled")))
        self.enterContext(patch("builtins.input", side_effect=AssertionError("interactive login disabled")))
        self.enterContext(patch.dict("os.environ", {"GOYGRAM_VAULT_KEY": base64.b64encode(bytes(range(32))).decode()}))
        self.config = RuntimeConfig(
            api_id=12345, api_hash="offline-hash", bot_token=None, owner_id=12345,
            prefix="!", session_name="fixture", session_dir=self.root / "sessions",
            state_path=self.root / "state.sqlite3", backup_keep=7,
        )

    def build(self, **values):
        runtime = Runtime(replace(self.config, **values))
        self.addAsyncCleanup(runtime.close)
        runtime.build()
        return runtime

    def message(self, runtime, **values):
        raw = {"msg_id": 71, "chat_id": 12345, "from_id": 12345, "text": "!probe", "is_me": True}
        raw.update(values)
        return MsgObj("mt" if runtime.app.mt is not None else "bot", raw, runtime.app.core)

    def request(self, runtime):
        core = runtime.app.core
        if core.mt is not None:
            self.enterContext(patch.object(core.mt, "resolve_peer", new=AsyncMock(return_value={"_": "inputPeerSelf"})))
            return self.enterContext(patch.object(core.mt, "call", new=AsyncMock(return_value={"ok": True, "result": {}})))
        return self.enterContext(patch.object(core, "bot_req", new=AsyncMock(return_value={"ok": True, "result": {}})))
