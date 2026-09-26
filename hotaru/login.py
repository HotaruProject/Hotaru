from __future__ import annotations

import asyncio
import base64
import io
import importlib
import logging
import os
import re
import secrets
import select as _select
import signal
import shutil
import sys
import termios
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Sequence, TypedDict, cast

from goygram.errors import RPCError


AUTH_TIMEOUT = 30.0
SESSION_ERRORS = frozenset({
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_PERM_EMPTY",
    "AUTH_KEY_DUPLICATED",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
})

log = logging.getLogger(__name__)


async def check_session(app: Any) -> bool:
    try:
        await asyncio.wait_for(
            app.mt_users_get_users(id=[{"_": "inputUserSelf"}], retry=0),
            timeout=AUTH_TIMEOUT,
        )
    except RPCError as exc:
        if exc.message not in SESSION_ERRORS:
            raise
        mt = app.mt
        await asyncio.wait_for(mt.close(), timeout=AUTH_TIMEOUT)
        if app.session.path is not None:
            app.session.path.unlink(missing_ok=True)
        app.session.data.clear()
        app.core.self_id = 0
        mt.self_id = 0
        mt.auth_key = None
        mt.auth_ready.clear()
        mt.server_salt = b"\x00" * 8
        mt.session_id = secrets.token_bytes(8)
        mt.seq = 0
        mt._init_done = False
        mt.buf.clear()
        mt.dc_auth_keys.clear()
        mt.entities.clear()
        mt.entity_usernames.clear()
        log.warning("Session invalid; deleted. Log in again.")
        return False
    return True


_CAPTION = "Hotaru Userbot"
_ART_SIZES = (72, 52, 34)
_ART_CACHE: dict[int, list[str]] = {}


def _art_dir() -> Path:
    bundled = Path(__file__).resolve().parent / "art"
    return bundled if bundled.is_dir() else Path(__file__).resolve().parent.parent / "art"


def _art(term: int) -> list[str]:
    cols = min(term - 4, _ART_SIZES[0])
    pick = next((size for size in _ART_SIZES if size <= cols), None)
    if pick is None:
        return []
    if pick not in _ART_CACHE:
        _ART_CACHE[pick] = (_art_dir() / f"quasar-{pick}.ans").read_text(encoding="utf-8").split("\n")
    return _ART_CACHE[pick]


def _banner_text() -> str:
    term = columns()
    art = _art(term)
    if not art:
        return style("dim", _CAPTION)
    size = max(width(line) for line in art)
    pad = " " * max(0, (term - size) // 2)
    caption = pad + " " * max(0, (size - len(_CAPTION)) // 2) + style("dim", _CAPTION)
    return "\n".join([*(pad + line for line in art), caption])


# --------------------------------------------------------------------------- #
# prompt widgets (frame layout, glyphs and colours: @clack/prompts 1.8.1, MIT)
# --------------------------------------------------------------------------- #
class Diff(TypedDict):
    lines: list[int]
    numLinesBefore: int
    numLinesAfter: int


Options = Sequence[Dict[str, Any]]
Validate = Callable[[str], Any]

# --------------------------------------------------------------------------- #
# styles: node:util styleText semantics (each style closes with its own code)
# --------------------------------------------------------------------------- #
_CODES = {
    "reset": (0, 0),
    "bold": (1, 22),
    "dim": (2, 22),
    "italic": (3, 23),
    "underline": (4, 24),
    "inverse": (7, 27),
    "hidden": (8, 28),
    "strikethrough": (9, 29),
    "black": (30, 39),
    "red": (31, 39),
    "green": (32, 39),
    "yellow": (33, 39),
    "blue": (34, 39),
    "magenta": (35, 39),
    "cyan": (36, 39),
    "white": (37, 39),
    "gray": (90, 39),
    "bgBlack": (40, 49),
    "bgRed": (41, 49),
    "bgGreen": (42, 49),
    "bgYellow": (43, 49),
    "bgBlue": (44, 49),
    "bgMagenta": (45, 49),
    "bgCyan": (46, 49),
    "bgWhite": (47, 49),
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def style(spec: str | Sequence[str], text: str) -> str:
    names = [spec] if isinstance(spec, str) else list(spec)
    left, right = "", ""
    for name in names:
        open_code, close = _CODES[name]
        left += f"\x1b[{open_code}m"
        right = f"\x1b[{close}m" + right
    return f"{left}{text}{right}"


def width(text: str) -> int:
    plain = ANSI_RE.sub("", text)
    total = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def columns() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def rows() -> int:
    return shutil.get_terminal_size((80, 24)).lines


# --------------------------------------------------------------------------- #
# guide symbols (packages/prompts/src/common.ts)
# --------------------------------------------------------------------------- #
S_STEP_ACTIVE = "◆"
S_STEP_CANCEL = "■"
S_STEP_ERROR = "▲"
S_STEP_SUBMIT = "◇"
S_BAR_START = "┌"
S_BAR = "│"
S_BAR_END = "└"
S_RADIO_ACTIVE = "●"
S_RADIO_INACTIVE = "○"
S_PASSWORD_MASK = "▪"
S_INFO = "●"
S_SUCCESS = "◆"
S_WARN = "▲"
S_ERROR = "■"
S_BAR_H = "─"
S_CORNER_TOP_RIGHT = "╮"
S_CONNECT_LEFT = "├"
S_CORNER_BOTTOM_RIGHT = "╯"
S_CORNER_BOTTOM_LEFT = "╰"
S_CORNER_TOP_LEFT = "╭"

SELECT_INSTRUCTIONS = [f"{style('dim', '↑/↓')} to navigate", f"{style('dim', 'Enter:')} confirm"]
MULTISELECT_INSTRUCTIONS = [
    f"{style('dim', '↑/↓')} to navigate",
    f"{style('dim', 'Space:')} select",
    f"{style('dim', 'Enter:')} confirm",
]

ALIASES = {"k": "up", "j": "down", "h": "left", "l": "right", "\x03": "cancel", "escape": "cancel"}

CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
ERASE_DOWN = "\x1b[J"


def move(x: int = 0, y: int = 0) -> str:
    out = ""
    if x < 0:
        out += f"\x1b[{-x}D"
    elif x > 0:
        out += f"\x1b[{x}C"
    if y < 0:
        out += f"\x1b[{-y}A"
    elif y > 0:
        out += f"\x1b[{y}B"
    return out


def erase_lines(count: int) -> str:
    clear = ""
    for index in range(count):
        clear += "\x1b[2K" + ("\x1b[1A" if index < count - 1 else "")
    if count:
        clear += "\x1b[G"
    return clear


def diff_lines(before: str, after: str) -> Diff | None:
    if before == after:
        return None
    old, new = before.split("\n"), after.split("\n")
    lines = [
        index
        for index in range(max(len(old), len(new)))
        if (old[index] if index < len(old) else None) != (new[index] if index < len(new) else None)
    ]
    return {"lines": lines, "numLinesBefore": len(old), "numLinesAfter": len(new)}


def symbol(state: str) -> str:
    if state in ("initial", "active"):
        return style("cyan", S_STEP_ACTIVE)
    if state == "cancel":
        return style("red", S_STEP_CANCEL)
    if state == "error":
        return style("yellow", S_STEP_ERROR)
    if state == "submit":
        return style("green", S_STEP_SUBMIT)
    if state == "validating":
        return style("dim", S_STEP_ACTIVE)
    return ""


def symbol_bar(state: str) -> str:
    colour = {"initial": "cyan", "active": "cyan", "cancel": "red", "error": "yellow", "submit": "green"}.get(state)
    return style(colour, S_BAR) if colour else ""


def wrap_ansi(text: str, limit: int) -> list[str]:
    """Wrap to `limit` visible columns; escape sequences are carried along (wrapAnsi hard/trim=false)."""
    lines: list[str] = []
    for raw in text.split("\n"):
        current: list[str] = []
        size = 0
        for token in re.split(r"(\x1b\[[0-9;]*m)", raw):
            if token.startswith("\x1b["):
                current.append(token)
                continue
            for ch in token:
                step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                if size + step > limit:
                    lines.append("".join(current))
                    current, size = [], 0
                current.append(ch)
                size += step
        lines.append("".join(current))
    return lines


def wrap_text_with_prefix(
    text: str,
    prefix: str,
    start_prefix: str | None = None,
    end_prefix: str | None = None,
    columns_limit: int | None = None,
) -> str:
    limit = (columns_limit or columns()) - width(prefix)
    lines = wrap_ansi(text, limit)
    start_prefix = prefix if start_prefix is None else start_prefix
    end_prefix = prefix if end_prefix is None else end_prefix
    out: list[str] = []
    for index, line in enumerate(lines):
        if index == 0:
            out.append(f"{start_prefix}{line}")
        elif index == len(lines) - 1:
            out.append(f"{end_prefix}{line}")
        else:
            out.append(f"{prefix}{line}")
    return "\n".join(out)


def format_instruction_footer(instructions: Sequence[str]) -> list[str]:
    return [
        f"{style('cyan', S_BAR)}  " + " • ".join(instructions),
        style("cyan", S_BAR_END),
    ]


def limit_options(
    options: Sequence[Any],
    cursor: int,
    styler: Callable[[Any, bool], str],
    max_items: float = float("inf"),
    column_padding: int = 0,
    row_padding: int = 4,
) -> list[str]:
    """Port of packages/prompts/src/limit-options.ts (sliding window + ellipsis)."""
    available_columns = columns() - column_padding
    output_max_items = max(rows() - row_padding, 0)
    computed = int(max(min(max_items, output_max_items), 5))
    location = 0
    if cursor >= computed - 3:
        location = max(min(cursor - computed + 3, len(options) - computed), 0)
    top_ellipsis = computed < len(options) and location > 0
    bottom_ellipsis = computed < len(options) and location + computed < len(options)
    end = min(location + computed, len(options))
    window_start = location + (1 if top_ellipsis else 0)
    window_end = end - (1 if bottom_ellipsis else 0)

    groups: list[list[str]] = []
    line_count = 0
    if top_ellipsis:
        line_count += 1
    if bottom_ellipsis:
        line_count += 1
    for index in range(window_start, window_end):
        item = options[index]
        styled = styler(item, index == cursor) if item is not None else ""
        wrapped = wrap_ansi(styled, available_columns)
        groups.append(wrapped)
        line_count += len(wrapped)

    if line_count > output_max_items:
        adjusted = output_max_items
        cursor_group = cursor - window_start

        def trim(start: int, stop: int, from_end: bool = False) -> tuple[int, int]:
            kept = line_count
            removed = 0
            indexes = range(stop - 1, start - 1, -1) if from_end else range(start, stop)
            for index in indexes:
                group = groups[index]
                if group:
                    kept -= len(group)
                removed += 1
                if kept <= adjusted:
                    break
            return kept, removed

        if top_ellipsis:
            kept, preceding = trim(0, cursor_group)
            if kept > adjusted:
                if not bottom_ellipsis:
                    adjusted -= 1
                kept, following = trim(cursor_group + 1, len(groups), True)
            else:
                following = 0
        else:
            if not bottom_ellipsis:
                adjusted -= 1
            kept, following = trim(cursor_group + 1, len(groups), True)
            preceding = 0
            if kept > adjusted:
                adjusted -= 1
                kept, preceding = trim(0, cursor_group)
        if preceding > 0:
            top_ellipsis = True
            del groups[:preceding]
        if following > 0:
            bottom_ellipsis = True
            del groups[len(groups) - following:]

    result: list[str] = []
    if top_ellipsis:
        result.append(style("dim", "..."))
    for group in groups:
        result.extend(group)
    if bottom_ellipsis:
        result.append(style("dim", "..."))
    return result


# --------------------------------------------------------------------------- #
# raw terminal input
# --------------------------------------------------------------------------- #
class Key:
    __slots__ = ("name", "char")

    def __init__(self, name: str, char: str = ""):
        self.name = name
        self.char = char


_ESCAPES = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1b[Z": "tab"}


def write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


_prelude: Callable[[], str] | None = None
_block_log: list[str] = []
_WAKE_R, _WAKE_W = os.pipe()
os.set_blocking(_WAKE_R, False)
os.set_blocking(_WAKE_W, False)
_resize_armed = False


def set_prelude(render: Callable[[], str] | None) -> None:
    global _prelude
    _prelude = render
    del _block_log[:]


def _block_write(text: str) -> None:
    _block_log.append(text)
    write(text)


def print_prelude() -> None:
    """Draw the banner. The header lines written after it stay in _block_log for repaints."""
    text = _prelude() if _prelude is not None else ""
    if text:
        write(text + "\n")


def _on_winch(*_args: object) -> None:
    return None


def _install_resize() -> None:
    global _resize_armed
    if _resize_armed:
        return
    _resize_armed = True
    signal.set_wakeup_fd(_WAKE_W)
    signal.signal(signal.SIGWINCH, _on_winch)


def _resize_pending() -> bool:
    try:
        return os.read(_WAKE_R, 64) != b""
    except (BlockingIOError, OSError):
        return False


class Raw:
    """Raw stdin, but output keeps ONLCR: upstream writes LF and the tty adds CR."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self) -> Raw:
        if not sys.stdin.isatty():
            return self
        _install_resize()
        _resize_pending()
        self.saved = termios.tcgetattr(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~(termios.IXON | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.BRKINT)
        attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)
        return self

    def __exit__(self, *exc: object) -> bool:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return False

    def key(self, timeout: float | None = None) -> Key | None:
        ready, _, _ = _select.select([self.fd, _WAKE_R], [], [], timeout)
        if _WAKE_R in ready and _resize_pending():
            return Key("resize", "")
        if self.fd not in ready:
            return None
        data = os.read(self.fd, 1)
        if not data:
            return None
        char = data.decode("utf-8", "replace")
        if char == "\x1b":
            while True:
                ready, _, _ = _select.select([self.fd], [], [], 0.05)
                if not ready:
                    break
                more = os.read(self.fd, 1).decode("utf-8", "replace")
                char += more
                if char in _ESCAPES:
                    break
            name = _ESCAPES.get(char, "escape" if len(char) == 1 else char)
            return Key(name, "")
        if char in ("\r", "\n"):
            return Key("return", char)
        if char in ("\x7f", "\x08"):
            return Key("backspace", char)
        if char == "\t":
            return Key("tab", char)
        if char == "\x03":
            return Key("cancel", char)
        if char == " ":
            return Key("space", char)
        return Key("char", char)


# --------------------------------------------------------------------------- #
# prompt base (packages/core/src/prompts/prompt.ts)
# --------------------------------------------------------------------------- #
class Cancelled(KeyboardInterrupt):
    pass


class Prompt:
    state: str
    error: str
    value: Any
    user_input: str
    cursor: int
    _prev_frame: str
    _resolved: bool

    def __init__(self, validate: Validate | None = None, track: bool = True, initial: Any = None) -> None:
        self.state = "initial"
        self.error = ""
        self.value = initial
        self.user_input = ""
        self.cursor = 0
        self._track = track
        self._validate = validate
        self._prev_frame = ""
        self._raw = None
        self._resolved = False

    # ---- frame plumbing (upstream Prompt.render) -------------------------- #
    def render(self) -> str:
        raise NotImplementedError

    def restore_cursor(self) -> None:
        lines = self._prev_frame.count("\n")
        write(move(-999, lines * -1))

    def draw(self) -> None:
        frame = self.render()
        if frame == self._prev_frame:
            return
        if self.state == "initial":
            write(CURSOR_HIDE)
        else:
            diff = diff_lines(self._prev_frame, frame)
            total_rows = rows()
            self.restore_cursor()
            if diff is not None:
                offset_after = max(0, diff["numLinesAfter"] - total_rows)
                offset_before = max(0, diff["numLinesBefore"] - total_rows)
                line = next((item for item in diff["lines"] if item >= offset_after), None)
                if line is None:
                    self._prev_frame = frame
                    return
                if len(diff["lines"]) == 1:
                    write(move(0, line - offset_before))
                    write(erase_lines(1))
                    lines = frame.split("\n")
                    write(lines[line])
                    self._prev_frame = frame
                    write(move(0, len(lines) - line - 1))
                    return
                if len(diff["lines"]) > 1:
                    if offset_after < offset_before:
                        line = offset_after
                    else:
                        adjusted = line - offset_before
                        if adjusted > 0:
                            write(move(0, adjusted))
                    write(ERASE_DOWN)
                    write("\n".join(frame.split("\n")[line:]))
                    self._prev_frame = frame
                    return
            write(ERASE_DOWN)
        write(frame)
        if self.state == "initial":
            self.state = "active"
        self._prev_frame = frame

    def close(self) -> None:
        write("\n")

    def _on_resize(self) -> None:
        """Rewrapped lines make row arithmetic unusable: repaint the block from a clean screen."""
        header = list(_block_log)
        write("\x1b[2J\x1b[H")
        print_prelude()
        for chunk in header:
            write(chunk)
        self._prev_frame = ""
        self.draw()

    def _resolve(self) -> None:
        """upstream's once('submit'/'cancel') handler: the cursor is shown exactly once."""
        if self._resolved:
            return
        self._resolved = True
        write(CURSOR_SHOW)

    # ---- interaction ----------------------------------------------------- #
    def handle(self, key: Key) -> bool:
        """Override; returns True when the prompt is finished."""
        raise NotImplementedError

    def run(self) -> Any:
        try:
            with Raw() as raw:
                self._raw = raw
                self.draw()
                while True:
                    key = raw.key()
                    if key is None:
                        continue
                    if key.name == "resize":
                        self._on_resize()
                        continue
                    done = self.handle(key)
                    self.draw()
                    if done:
                        break
        finally:
            self.close()
            self._resolve()
        if self.state == "cancel":
            raise Cancelled
        return self.value

    # ---- helpers --------------------------------------------------------- #
    def _submit(self) -> bool:
        if self._validate is not None:
            problem = self._validate(self.value)
            if problem:
                self.error = str(problem)
                self.state = "error"
                return False
        self.state = "submit"
        return True

    def user_input_with_cursor(self) -> str:
        return ""

    def _move(self, action: str) -> None:
        if action == "left" and self.cursor > 0:
            self.cursor -= 1
        elif action == "right" and self.cursor < len(self.user_input):
            self.cursor += 1

    def _type(self, char: str) -> None:
        self.user_input = self.user_input[: self.cursor] + char + self.user_input[self.cursor:]
        self.cursor += 1


class InputPrompt(Prompt):
    """text + password share their key handling."""

    def __init__(
        self,
        message: str,
        placeholder: str | None = None,
        validate: Validate | None = None,
        mask: str | None = None,
        initial: Any = None,
        default_value: str | None = None,
    ) -> None:
        super().__init__(validate=validate, initial=initial)
        self.message = message
        self.placeholder = placeholder
        self.mask = mask
        self.default_value = default_value

    def user_input_with_cursor(self) -> str:
        if not self.user_input:
            return style(["inverse", "hidden"], "_")
        if self.cursor >= len(self.user_input):
            return f"{self.user_input}█"
        head = self.user_input[: self.cursor]
        char = self.user_input[self.cursor]
        tail = self.user_input[self.cursor + 1:]
        return f"{head}{style('inverse', char)}{tail}"

    def _masked(self) -> str:
        return re.sub(".", self.mask, self.user_input) if self.mask else ""

    def placeholder_text(self) -> str:
        if self.placeholder:
            return style("inverse", self.placeholder[0]) + style("dim", self.placeholder[1:])
        return style(["inverse", "hidden"], "_")

    def title(self) -> str:
        return f"{style('gray', S_BAR)}\n{symbol(self.state)}  {self.message}\n"

    def render(self) -> str:
        raise NotImplementedError

    def handle(self, key: Key) -> bool:
        if self.state == "error":
            self.state = "active"
        if key.name == "return":
            if self.state == "error" and not self.user_input:
                return False
            if self._submit():
                return True
            return False
        if ALIASES.get(key.name, key.name) == "cancel":
            self.state = "cancel"
            return True
        if key.name == "backspace":
            if self.cursor > 0:
                self.user_input = self.user_input[: self.cursor - 1] + self.user_input[self.cursor:]
                self.cursor -= 1
            return False
        if key.name in ("left", "right"):
            self._move(key.name)
            return False
        if key.name == "up" or key.name == "down":
            return False
        if key.name == "char" or key.name == "space":
            self._type(key.char)
            return False
        return False


class TextPrompt(InputPrompt):
    def render(self) -> str:
        user_input = self.user_input_with_cursor() if self.user_input else self.placeholder_text()
        value = self.value or ""
        if self.state == "error":
            error_text = f"  {style('yellow', self.error)}" if self.error else ""
            prefix = style("yellow", S_BAR)
            return f"{self.title().rstrip()}\n{prefix}  {user_input}\n{style('yellow', S_BAR_END)}{error_text}\n"
        if self.state == "submit":
            value_text = f"  {style('dim', value)}" if value else ""
            return f"{self.title()}{style('gray', S_BAR)}{value_text}"
        if self.state == "cancel":
            value_text = f"  {style(['strikethrough', 'dim'], value)}" if value else ""
            prefix = style("gray", S_BAR)
            tail = f"\n{prefix}" if value.strip() else ""
            return f"{self.title()}{prefix}{value_text}{tail}"
        return (
            f"{self.title()}{style('cyan', S_BAR)}  {user_input}\n{style('cyan', S_BAR_END)}\n"
        )

    def handle(self, key: Key) -> bool:
        if key.name == "return" and self.state != "error":
            self.value = self.user_input or self.default_value or ""
        return super().handle(key)


class PasswordPrompt(InputPrompt):
    def user_input_with_cursor(self) -> str:
        if self.state in ("submit", "cancel"):
            return self._masked()
        if self.cursor >= len(self.user_input):
            return f"{self._masked()}{style(['inverse', 'hidden'], '_')}"
        head = self._masked()[: self.cursor]
        char = self._masked()[self.cursor]
        tail = self._masked()[self.cursor + 1:]
        return f"{head}{style('inverse', char)}{tail}"

    def render(self) -> str:
        user_input = self.user_input_with_cursor()
        masked = self._masked()
        if self.state == "error":
            prefix = style("yellow", S_BAR)
            prefix_end = style("yellow", S_BAR_END)
            return f"{self.title().rstrip()}\n{prefix}  {masked}\n{prefix_end}  {style('yellow', self.error)}\n"
        if self.state == "submit":
            masked_text = style("dim", masked) if masked else ""
            return f"{self.title()}{style('gray', S_BAR)}  {masked_text}"
        if self.state == "cancel":
            masked_text = style(["strikethrough", "dim"], masked) if masked else ""
            tail = f"\n{style('gray', S_BAR)}" if masked else ""
            return f"{self.title()}{style('gray', S_BAR)}  {masked_text}{tail}"
        return f"{self.title()}{style('cyan', S_BAR)}  {user_input}\n{style('cyan', S_BAR_END)}\n"

    def handle(self, key: Key) -> bool:
        if key.name == "return" and self.state != "error":
            self.value = self.user_input
        return super().handle(key)


# --------------------------------------------------------------------------- #
# select / multiselect / confirm
# --------------------------------------------------------------------------- #
class SelectPrompt(Prompt):
    def __init__(
        self,
        message: str,
        options: Options,
        initial: Any = None,
        max_items: float | None = None,
        show_instructions: bool = True,
    ) -> None:
        super().__init__()
        self.message = message
        self.items = options
        self.max_items = max_items if max_items is not None else float("inf")
        self.show_instructions = show_instructions
        self.index = 0
        if initial is not None:
            for position, item in enumerate(options):
                if item.get("value") == initial:
                    self.index = position
        self.value = options[self.index].get("value") if options else None

    def opt(self, option: dict[str, Any] | None, state: str) -> str:
        if option is None:
            return ""
        label = option.get("label", str(option.get("value")))
        hint = option.get("hint")
        if state == "disabled":
            hint_text = f"({hint})" if hint else "(disabled)"
            text = f"{style('gray', S_RADIO_INACTIVE)} {style('gray', label)}"
            return text + (f" {style('dim', hint_text)}" if hint else "")
        if state == "selected":
            return style("dim", label)
        if state == "cancelled":
            return style(["strikethrough", "dim"], label)
        if state == "active":
            text = f"{style('green', S_RADIO_ACTIVE)} {label}"
            return text + (f" {style('dim', f'({hint})')}" if hint else "")
        return f"{style('dim', S_RADIO_INACTIVE)} {style('dim', label)}"

    def title(self) -> str:
        message = wrap_text_with_prefix(self.message, f"{symbol_bar(self.state)}  ", f"{symbol(self.state)}  ")
        return f"{style('gray', S_BAR)}\n{message}\n"

    def footer(self) -> str:
        lines = format_instruction_footer(SELECT_INSTRUCTIONS) if self.show_instructions else [style("cyan", S_BAR_END)]
        return "\n".join(lines)

    def render(self) -> str:
        if self.state == "submit":
            selected = self.opt(self.items[self.index], "selected")
            return f"{self.title()}{style('gray', S_BAR)}  {selected}"
        if self.state == "cancel":
            cancelled = self.opt(self.items[self.index], "cancelled")
            return f"{self.title()}{style('gray', S_BAR)}  {cancelled}\n{style('gray', S_BAR)}"
        prefix = f"{style('cyan', S_BAR)}  "
        body = limit_options(
            self.items,
            self.index,
            lambda item, active: self.opt(item, "active" if active else "inactive"),
            max_items=self.max_items,
            column_padding=width(prefix),
            row_padding=len(self.title().split("\n")) + 2,
        )
        return f"{self.title()}{prefix}" + f"\n{prefix}".join(body) + f"\n{self.footer()}\n"

    def handle(self, key: Key) -> bool:
        name = ALIASES.get(key.name, key.name)
        if key.name == "return":
            self.value = self.items[self.index].get("value")
            self.state = "submit"
            return True
        if name == "cancel":
            self.state = "cancel"
            return True
        if name == "up":
            self.index = self.index - 1 if self.index > 0 else len(self.items) - 1
        elif name == "down":
            self.index = self.index + 1 if self.index < len(self.items) - 1 else 0
        return False


def cancel_pressed(keys: Raw) -> bool:
    """True when Esc / Ctrl+C is waiting in the input buffer (upstream block() cancel hook)."""
    while True:
        key = keys.key(timeout=0)
        if key is None:
            return False
        if ALIASES.get(key.name, key.name) == "cancel":
            return True


def intro(title: str = "") -> None:
    _block_write(f"{style('gray', S_BAR_START)}  {title}\n")


def outro(message: str = "") -> None:
    _block_write(f"{style('gray', S_BAR)}\n{style('gray', S_BAR_END)}  {message}\n\n")


def cancel(message: str = "Operation cancelled.") -> None:
    _block_write(f"{style('gray', S_BAR_END)}  {style('red', message)}\n\n")


def note(message: str = "", title: str = "") -> None:
    limit = columns() - 6
    lines = ["", *wrap_ansi(message, limit), ""]
    title_len = width(title)
    longest = max([width(line) for line in lines] + [title_len]) + 2
    body = "\n".join(
        f"{style('gray', S_BAR)}  {line}{' ' * (longest - width(line))}{style('gray', S_BAR)}" for line in lines
    )
    bar = S_BAR_H * max(longest - title_len - 1, 1) + S_CORNER_TOP_RIGHT
    _block_write(
        f"{style('gray', S_BAR)}\n{style('green', S_STEP_SUBMIT)}  {style('reset', title)} "
        f"{style('gray', bar)}\n{body}\n"
        f"{style('gray', S_CONNECT_LEFT + S_BAR_H * (longest + 2) + S_CORNER_BOTTOM_RIGHT)}\n"
    )


def _log(message: str | list[str], symbol: str, spacing: int = 1) -> None:
    parts = [style("gray", S_BAR)] * spacing
    lines = message if isinstance(message, list) else str(message).split("\n")
    if lines:
        first, rest = lines[0], lines[1:]
        parts.append(f"{symbol}  {first}" if first else symbol)
        for line in rest:
            parts.append(f"{style('gray', S_BAR)}  {line}" if line else style("gray", S_BAR))
    _block_write("\n".join(parts) + "\n")


def log_info(message: str) -> None:
    _log(message, style("blue", S_INFO))


def log_warn(message: str) -> None:
    _log(message, style("yellow", S_WARN))


def log_error(message: str) -> None:
    _log(message, style("red", S_ERROR))


def spinner() -> Spinner:
    return Spinner()


class Spinner:
    FRAMES = ["◒", "◐", "◓", "◑"]
    DELAY = 0.08

    def __init__(self) -> None:
        self.message = ""
        self._index = 0
        self._dots = 0.0
        self._active = False
        self._prev = None
        self._last = 0.0

    def start(self, message: str = "") -> None:
        self.message = re.sub(r"\.+$", "", message)
        self._active = True
        write(CURSOR_HIDE)  # upstream block() hides the cursor while spinning
        _block_write(f"{style('gray', S_BAR)}\n")
        self._last = time.monotonic()  # first frame after DELAY, like upstream's setInterval

    def _clear(self) -> None:
        if self._prev is None:
            return
        lines = self._prev.count("\n") + 1
        if lines > 1:
            write(f"\x1b[{lines - 1}A")
        write("\x1b[1G" + ERASE_DOWN)

    def _paint(self) -> None:
        frame = style("magenta", self.FRAMES[self._index])
        dots = "." * int(self._dots)
        line = f"{frame}  {self.message}{dots}"
        self._clear()
        write(line)
        self._prev = line
        self._index = self._index + 1 if self._index + 1 < len(self.FRAMES) else 0
        self._dots = self._dots + 0.125 if self._dots < 4 else 0
        self._last = time.monotonic()

    def tick(self) -> None:
        if not self._active:
            return
        now = time.monotonic()
        if now - self._last >= self.DELAY:
            self._paint()

    def _stop(self, message: str, state: str) -> None:
        if not self._active:
            return
        self._active = False
        self._clear()
        symbol_ = {"submit": style("green", S_STEP_SUBMIT), "cancel": style("red", S_STEP_CANCEL), "error": style("red", S_STEP_ERROR)}[state]
        _block_write(f"{symbol_}  {message or self.message}\n")
        write(CURSOR_SHOW)  # upstream unblock() shows the cursor again
        self._prev = None

    def stop(self, message: str = "") -> None:
        self._stop(message, "submit")

    def cancel(self, message: str = "") -> None:
        self._stop(message or "Canceled", "cancel")

    def error(self, message: str = "") -> None:
        self._stop(message or "Something went wrong", "error")

    def clear(self) -> None:
        if not self._active:
            return
        self._active = False
        self._clear()
        self._prev = None

    def message_(self, message: str = "") -> None:
        self.message = re.sub(r"\.+$", "", message)


# --------------------------------------------------------------------------- #
# the demo: examples/basic/index.ts, verbatim
# --------------------------------------------------------------------------- #
def text(
    message: str,
    placeholder: str | None = None,
    validate: Validate | None = None,
    default_value: str | None = None,
    **kw: Any,
) -> Any:
    return TextPrompt(message, placeholder=placeholder, validate=validate, default_value=default_value).run()


def password(message: str, validate: Validate | None = None, mask: str = S_PASSWORD_MASK, **kw: Any) -> Any:
    return PasswordPrompt(message, validate=validate, mask=mask).run()


def select(message: str, options: Options, initial: Any = None, max_items: float | None = None, **kw: Any) -> Any:
    return SelectPrompt(message, options, initial, max_items).run()


def _ask(
    label: str,
    *,
    secret: bool = False,
    default: str | None = None,
    placeholder: str | None = None,
    validate: Callable[[str], str | None] | None = None,
) -> str:
    if secret:
        return str(password(label, validate=validate)).strip()
    return str(text(label, placeholder=placeholder, default_value=default, validate=validate)).strip()


async def _spin(message: str, awaitable: Awaitable[Any]) -> Any:
    spin = spinner()
    spin.start(message)
    task = asyncio.ensure_future(awaitable)
    try:
        with Raw() as keys:
            while not task.done():
                spin.tick()
                if cancel_pressed(keys):
                    spin.cancel("Cancelled.")
                    task.cancel()
                    raise Cancelled
                await asyncio.sleep(0.02)
        result = task.result()
    except BaseException:
        spin.clear()
        raise
    spin.stop(message)
    return result


def _api_id_problem(raw: str) -> str | None:
    return None if raw.strip().isdigit() else "API ID is a number, e.g. 1234567"


def _api_hash_problem(raw: str) -> str | None:
    return None if len(raw.strip()) >= 16 else "API hash looks too short"


def _prefix_problem(raw: str) -> str | None:
    return None if len(raw) <= 1 else "Prefix is a single character"


def _code_problem(raw: str) -> str | None:
    return None if raw.strip().isdigit() else "Telegram codes are digits"


def _phone_problem(raw: str) -> str | None:
    try:
        _phone(raw)
    except ValueError as exc:
        return str(exc)
    return None


def _phone(raw: str) -> str:
    text = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not text.startswith("+"):
        text = "+" + text.lstrip("+")
    if len(text) < 8:
        raise ValueError("phone is too short")
    return text


def collect_settings(state: Any) -> None:
    try:
        _collect_settings(state)
    except KeyboardInterrupt:
        cancel("Setup cancelled.")
        raise SystemExit(1) from None


def _collect_settings(state: Any) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("runtime settings are missing; run from an interactive TTY")
    set_prelude(_banner_text)
    print_prelude()
    intro(style("bgCyan", style("black", " hotaru setup ")))
    log_info("my.telegram.org → API ID / hash")
    api_id = int(_ask("API ID", placeholder="1234567", validate=_api_id_problem))
    api_hash = _ask("API hash", secret=True, validate=_api_hash_problem)
    prefix = _ask("Prefix", placeholder="!", default="!", validate=_prefix_problem) or "!"
    bot = _ask("Inline bot username", placeholder="empty = auto")
    import secrets
    values = {
        "api-id": api_id,
        "api-hash": api_hash,
        "bot-token": None,
        "owner-id": None,
        "prefix": prefix[:1],
        "session-name": f"hotaru-pending-{secrets.token_hex(8)}",
        "session-dir": ".",
        "backup-keep": 7,
    }
    for key, value in values.items():
        state.set_setting(key, value)
    wanted = bot.lstrip("@")
    if wanted:
        if not wanted.lower().endswith("bot"):
            wanted += "_bot" if "_" in wanted or wanted.isalnum() else "bot"
        state.set_setting("inline-bot-username-wanted", wanted)
    note(
        f"API ID     {api_id}\nprefix     {prefix}\ninline bot {wanted or 'auto'}\nsession    {values['session-dir']}",
        "settings saved",
    )
    outro("Starting Hotaru.")


def save_vault(app: Any, vault: Path, session_name: str, api_id: int, api_hash: str, user: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    security = importlib.import_module("goygram.security")
    _current_dc_id = cast(Callable[[Any], int | None], getattr(security, "_current_dc_id"))
    _extract_auth_blob = cast(Callable[[dict[str, Any]], bytes | None], getattr(security, "_extract_auth_blob"))
    _field = cast(Callable[..., Any], getattr(security, "_field"))
    _write_vault = cast(Callable[[Path, dict[str, Any], str], None], getattr(security, "_write_vault"))
    auth_blob = _extract_auth_blob(extra or {}) or getattr(app.mt, "auth_key", None)
    if auth_blob is None:
        raise RuntimeError("authorization did not return a session")
    payload = {
        "phone": user.get("phone", ""),
        "user": user,
        "auth_key": auth_blob.hex() if isinstance(auth_blob, (bytes, bytearray)) else str(auth_blob),
        "server_salt": app.mt.server_salt.hex() if getattr(app.mt, "server_salt", None) else "",
        "dc": _field(extra or {}, "dc_id", "dc") or _current_dc_id(app),
        "api_id": api_id,
        "api_hash": api_hash,
    }
    _write_vault(vault, payload, Path(session_name).name)
    uid = user.get("id", 0)
    if uid:
        app.self_id = uid
        app.mt.self_id = uid
    if hasattr(app, "session") and getattr(app, "session", None) is not None:
        app.session.data = payload


async def _phone_login(app: Any, api_id: int, api_hash: str) -> dict[str, Any]:
    from goygram import ext as rx
    security = importlib.import_module("goygram.security")
    _extract_error = cast(Callable[[dict[str, Any]], str | None], getattr(security, "_extract_error"))
    _extract_phone_code_hash = cast(Callable[[dict[str, Any]], str | None], getattr(security, "_extract_phone_code_hash"))
    _extract_user = cast(Callable[[Any], dict[str, Any] | None], getattr(security, "_extract_user"))
    _mt_req_with_migrate = cast(Callable[..., Awaitable[dict[str, Any]]], getattr(security, "_mt_req_with_migrate"))
    while True:
        phone = _phone(_ask("Phone number", placeholder="+1 555 000 1234", validate=_phone_problem))
        settings = rx.serialize_constructor("codeSettings", {"flags": 0})
        sent = await _spin("Sending the code", _mt_req_with_migrate(app, "auth_send_code", phone_number=phone, api_id=api_id, api_hash=api_hash, settings=settings))
        err = _extract_error(sent)
        if err and "SESSION_PASSWORD_NEEDED" not in err:
            log_error(err)
            continue
        code_hash = _extract_phone_code_hash(sent)
        if not code_hash:
            log_error("no code hash")
            continue
        while True:
            code = _ask("Code", placeholder="12345", validate=_code_problem)
            try:
                sign = await _spin("Signing in", _mt_req_with_migrate(app, "auth_sign_in", phone_number=phone, phone_code=code, phone_code_hash=code_hash, api_id=api_id, api_hash=api_hash))
                sign_err = ""
            except Exception as exc:
                sign, sign_err = None, str(exc)
            else:
                sign_err = _extract_error(sign) or ""
            if "PHONE_CODE_INVALID" in sign_err or "CODE_INVALID" in sign_err:
                log_error("wrong code")
                continue
            final = sign
            if "SESSION_PASSWORD_NEEDED" in sign_err:
                while True:
                    pwd = _ask("2FA password", secret=True)
                    try:
                        check = await _spin("Checking the password", _mt_req_with_migrate(app, "auth_check_password", password=pwd, api_id=api_id, api_hash=api_hash))
                    except Exception as exc:
                        log_error(str(exc))
                        continue
                    err2 = _extract_error(check)
                    if err2:
                        log_error(err2)
                        continue
                    final = check
                    break
            elif sign_err:
                log_error(sign_err)
                continue
            user = _extract_user(final)
            if not user:
                log_error("no user in session")
                continue
            return {"user": user, "raw": final}


async def _qr_login(app: Any, api_id: int, api_hash: str) -> dict[str, Any] | None:
    from goygram.errors import GoyGramError
    security = importlib.import_module("goygram.security")
    _extract_error = cast(Callable[[dict[str, Any]], str | None], getattr(security, "_extract_error"))
    _extract_user = cast(Callable[[Any], dict[str, Any] | None], getattr(security, "_extract_user"))
    _mt_req_with_migrate = cast(Callable[..., Awaitable[dict[str, Any]]], getattr(security, "_mt_req_with_migrate"))
    while True:
        try:
            res = await _spin("Requesting a login token", _mt_req_with_migrate(app, "auth_export_login_token", api_id=api_id, api_hash=api_hash, except_ids=[]))
        except Exception as exc:
            log_error(str(exc))
            return None
        if not res.get("ok"):
            log_error(str(res))
            return None
        if res.get("type") == "loginToken":
            token = res["token"]
            b64 = base64.urlsafe_b64encode(token).decode().rstrip("=")
            url = f"tg://login?token={b64}"
            art = url
            try:
                import qrcode
                buf = io.StringIO()
                qr = qrcode.QRCode(border=1)
                qr.add_data(url)
                qr.print_ascii(out=buf)
                art = buf.getvalue()
            except Exception:
                pass
            note(url if "tg://login" in art else art, "scan in Telegram")
            expires = float(res.get("expires") or (time.time() + 30))
            app.mt.qr_update_ev.clear()
            spin = spinner()
            spin.start("Waiting for the scan")
            with Raw() as keys:
                try:
                    while time.time() < expires:
                        try:
                            await asyncio.wait_for(app.mt.qr_update_ev.wait(), timeout=0.1)
                        except asyncio.TimeoutError:
                            spin.tick()
                            if cancel_pressed(keys):
                                spin.cancel("Cancelled.")
                                raise Cancelled
                            continue
                        spin.tick()
                        app.mt.qr_update_ev.clear()
                        try:
                            poll = await _mt_req_with_migrate(app, "auth_export_login_token", api_id=api_id, api_hash=api_hash, except_ids=[])
                        except GoyGramError as exc:
                            if "SESSION_PASSWORD_NEEDED" not in str(exc):
                                continue
                            spin.stop("Scanned")
                            while True:
                                pwd = _ask("2FA password", secret=True)
                                try:
                                    check = await _spin("Checking the password", _mt_req_with_migrate(app, "auth_check_password", password=pwd, api_id=api_id, api_hash=api_hash))
                                except Exception as exc2:
                                    log_error(str(exc2))
                                    continue
                                err = _extract_error(check)
                                if err:
                                    log_error(err)
                                    continue
                                user = _extract_user(check)
                                if not user:
                                    continue
                                return {"user": user, "raw": check}
                            break
                        if poll.get("type") == "loginTokenSuccess":
                            user = _extract_user(poll)
                            if user:
                                spin.stop("Scanned")
                                return {"user": user, "raw": poll}
                    spin.stop("Code expired, new one")
                finally:
                    spin.clear()
            continue
        if res.get("type") == "loginTokenSuccess":
            user = _extract_user(res)
            if user:
                return {"user": user, "raw": res}
        return None


async def sign_in(runtime: Any) -> dict[str, str]:
    try:
        return await _sign_in(runtime)
    except KeyboardInterrupt:
        cancel("Login cancelled.")
        raise SystemExit(1) from None


async def _sign_in(runtime: Any) -> dict[str, str]:
    set_prelude(_banner_text)
    print_prelude()
    intro(style("bgCyan", style("black", " hotaru login ")))
    app = runtime.app.core
    config = runtime.config
    api_id = int(config.api_id)
    api_hash = str(config.api_hash)
    vault = runtime.app.session.path
    session_name = runtime.app.core.session_name
    if vault is None:
        raise RuntimeError("session path is missing")
    await _spin("Connecting to Telegram", app.mt.ensure_auth_key())
    method = select(
        "How do you want to sign in?",
        [
            {"value": "qr", "label": "QR code", "hint": "scan with the Telegram app"},
            {"value": "phone", "label": "Phone number"},
        ],
        initial="qr",
    )
    packed = None
    if method == "qr":
        packed = await _qr_login(app, api_id, api_hash)
        if packed is None:
            log_warn("QR failed, phone login")
    if packed is None:
        packed = await _phone_login(app, api_id, api_hash)
    from .accounts import parse_user_id
    uid = parse_user_id(config.session_name)
    if uid is not None and packed["user"].get("id") != uid:
        raise RuntimeError(f"Wrong account; log in as user {uid}.")
    save_vault(app, vault, session_name, api_id, api_hash, packed["user"], packed.get("raw"))
    if hasattr(runtime.app, "session"):
        runtime.app.session.data = getattr(app, "session", runtime.app.session).data if getattr(app, "session", None) else packed["raw"]
        if not runtime.app.session.data:
            runtime.app.session.data = {
                "user": packed["user"],
                "auth_key": app.mt.auth_key.hex() if app.mt.auth_key else "",
            }
    outro("Signed in. Starting Hotaru.")
    return {"source": "hotaru"}
