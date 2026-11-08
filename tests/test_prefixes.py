from __future__ import annotations

import ast
import asyncio
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hotaru.commands import CommandParser, valid_prefix
from hotaru.config import RuntimeConfig
from hotaru.kernel import Kernel


ROOT = Path(__file__).resolve().parents[1]


def parse(prefix, text, aliases=None, **kwargs):
    return CommandParser(prefix).parse(text, source="new", message_id=7, chat_id=42, aliases=aliases, **kwargs)


class PrefixParserTests(unittest.TestCase):
    def test_first_letter_of_every_builtin_command_is_a_valid_prefix(self):
        names = set()
        for path in (ROOT / "constellations").glob("*.hmod"):
            manifest = ast.literal_eval(ast.parse(path.read_text()).body[0].value)
            names.update(manifest.get("commands", []))
        self.assertTrue(names)
        for name in sorted(names):
            with self.subTest(command=name, prefix=name[0]):
                invocation = parse(name[0], name[0] + name)
                self.assertIsNotNone(invocation)
                self.assertEqual(invocation.name, name.casefold())

    def test_multicharacter_command_prefix_collisions(self):
        for prefix, name in (("he", "help"), ("hel", "help"), ("help", "help"), ("pi", "ping"), ("pin", "ping"), ("ping", "ping")):
            with self.subTest(prefix=prefix, command=name):
                invocation = parse(prefix, prefix + name)
                self.assertIsNotNone(invocation)
                self.assertEqual(invocation.name, name)

    def test_all_valid_leading_substrings_of_builtin_commands(self):
        names = set()
        for path in (ROOT / "constellations").glob("*.hmod"):
            names.update(ast.literal_eval(ast.parse(path.read_text()).body[0].value).get("commands", []))
        for name in sorted(names):
            for size in range(1, min(8, len(name)) + 1):
                prefix = name[:size]
                with self.subTest(command=name, prefix=prefix):
                    self.assertTrue(valid_prefix(prefix))
                    invocation = parse(prefix, prefix + name)
                    self.assertIsNotNone(invocation)
                    self.assertEqual(invocation.name, name)

    def test_numeric_unicode_underscore_and_mixed_prefixes(self):
        for prefix, name in (("1", "123"), ("12", "123"), ("р", "рудз"), ("你", "你好"), ("_", "_help"), ("h_", "h_help"), (".h", ".help"), ("h!", "h!help")):
            with self.subTest(prefix=prefix, command=name):
                self.assertTrue(valid_prefix(prefix))
                invocation = parse(prefix, prefix + name)
                self.assertIsNotNone(invocation)
                self.assertEqual(invocation.name, name)

    def test_symbolic_prefixes_still_parse_normal_commands(self):
        for prefix in (".", "!", "::", "!?", "🔥"):
            with self.subTest(prefix=prefix):
                self.assertEqual(parse(prefix, prefix + "help").name, "help")

    def test_doubled_symbolic_prefix_still_escapes_user_input(self):
        for prefix in (".", "!", "::", "!?", "🔥"):
            for suffix in ("help", " help", "ping arg"):
                with self.subTest(prefix=prefix, suffix=suffix):
                    self.assertIsNone(parse(prefix, prefix + prefix + suffix))

    def test_whitespace_separator_remains_supported(self):
        for prefix in ("h", "p", "he", ".", "::"):
            for separator in (" ", "\t", "\n", "  "):
                with self.subTest(prefix=prefix, separator=separator):
                    self.assertEqual(parse(prefix, prefix + separator + "help").name, "help")

    def test_prefix_matching_stays_case_sensitive(self):
        self.assertIsNone(parse("H", "hhelp"))
        self.assertIsNone(parse("h", "Hhelp"))
        for prefix in ("h", "H"):
            with self.subTest(prefix=prefix):
                invocation = parse(prefix, prefix + "HELP")
                self.assertIsNotNone(invocation)
                self.assertEqual(invocation.name, "help")

    def test_empty_and_unprefixed_input_is_ignored(self):
        for value in (None, "", "h", "h   ", "ping", " hhelp", 123):
            with self.subTest(value=value):
                self.assertIsNone(parse("h", value))

    def test_argument_spacing_and_metadata_are_preserved(self):
        message = object()
        invocation = parse("h", "hhelp   First  second\nthird", message=message)
        self.assertIsNotNone(invocation)
        self.assertEqual(invocation.name, "help")
        self.assertEqual(invocation.args, ("First", "second", "third"))
        self.assertEqual(invocation.raw_args, "First  second\nthird")
        self.assertEqual((invocation.source, invocation.message_id, invocation.chat_id), ("new", 7, 42))
        self.assertIs(invocation.message, message)

    def test_alias_targets_starting_with_the_prefix_work(self):
        for prefix, alias, target in (("h", "docs", "help"), ("p", "check", "ping"), ("he", "docs", "help")):
            with self.subTest(prefix=prefix, alias=alias, target=target):
                invocation = parse(prefix, prefix + alias + " arg", {alias: target})
                self.assertIsNotNone(invocation)
                self.assertEqual(invocation.name, target)
                self.assertEqual(invocation.args, ("arg",))

    def test_alias_names_starting_with_the_prefix_work(self):
        invocation = parse("h", "hhello arg", {"hello": "ping"})
        self.assertIsNotNone(invocation)
        self.assertEqual((invocation.name, invocation.args), ("ping", ("arg",)))

    def test_multiword_alias_and_expanded_raw_arguments(self):
        invocation = parse("h", "hshow help   First  second", {"show help": "help settings"})
        self.assertIsNotNone(invocation)
        self.assertEqual(invocation.name, "help")
        self.assertEqual(invocation.args, ("settings", "First", "second"))
        self.assertEqual(invocation.raw_args, "settings   First  second")

    def test_internal_alias_expansion_is_not_mistaken_for_escaped_input(self):
        invocation = parse(".", ".docs arg", {"docs": ".help"})
        self.assertIsNotNone(invocation)
        self.assertEqual((invocation.name, invocation.args), (".help", ("arg",)))

    def test_escaped_aliases_still_do_not_execute(self):
        self.assertIsNone(parse(".", "..docs", {"docs": "help"}))

    def test_alias_cycles_remain_bounded(self):
        self.assertEqual(parse("!", "!a", {"a": "b", "b": "a"}).name, "b")
        self.assertEqual(parse("h", "hdocs", {"docs": "docs"}).name, "docs")

    def test_prefix_setter_updates_escape_behavior_immediately(self):
        parser = CommandParser(".")
        self.assertIsNone(parser.command_name("..help"))
        parser.prefix = "h"
        self.assertEqual(parser.command_name("hhelp"), "help")
        parser.prefix = "p"
        self.assertEqual(parser.command_name("pping"), "ping")
        parser.prefix = "::"
        self.assertEqual(parser.command_name("::help"), "help")
        self.assertIsNone(parser.command_name("::::help"))

    def test_invalid_prefixes_remain_rejected(self):
        for prefix in ("", " ", "h p", "h\n", "123456789", None, 1):
            with self.subTest(prefix=prefix):
                self.assertFalse(valid_prefix(prefix))
                with self.assertRaises(ValueError):
                    CommandParser(prefix)

    def test_wrong_keyboard_layout_still_resolves_after_prefix_collision(self):
        parser = CommandParser("р")
        invocation = parser.parse("ррудз First", source="new", message_id=7, chat_id=42)
        self.assertIsNotNone(invocation)
        swapped = parser.swap_invocation(invocation)
        self.assertEqual(swapped.name, "help")
        self.assertTrue(swapped.layout_swapped)
        self.assertEqual(swapped.args, ("First",))


class PrefixIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, prefix, text, command, aliases=None, source="new", actor=1, outgoing=True):
        handler = AsyncMock(return_value=None)
        kernel = Kernel(parser=CommandParser(prefix), owner_id=1, context_factory=SimpleNamespace(create=lambda module_id, message: SimpleNamespace()))
        kernel.registry.register(command, handler, module_id="prefix-test")
        for name, target in (aliases or {}).items():
            kernel.registry.register_alias(name, target)
        message = SimpleNamespace(text=text, id=7, chat_id=42, from_id=actor, is_me=outgoing)
        try:
            await kernel.dispatch(message, source=source)
            running = tuple(kernel._running.values())
            if running:
                await asyncio.gather(*running)
            return handler, kernel.command_name(text)
        finally:
            await kernel.cancel_all()
            await kernel.tasks.close()

    async def test_hhelp_and_pping_reach_their_handlers(self):
        for prefix, command in (("h", "help"), ("p", "ping"), ("he", "help"), ("pin", "ping")):
            with self.subTest(prefix=prefix, command=command):
                handler, name = await self.exercise(prefix, prefix + command, command)
                handler.assert_awaited_once()
                self.assertEqual(name, command)

    async def test_alias_to_help_reaches_the_handler(self):
        handler, name = await self.exercise("h", "hdocs First", "help", {"docs": "help"})
        handler.assert_awaited_once()
        self.assertEqual(name, "help")
        self.assertEqual(handler.await_args.args[1].args, ("First",))

    async def test_keyboard_layout_fallback_reaches_the_handler(self):
        handler, name = await self.exercise("р", "ррудз", "help")
        handler.assert_awaited_once()
        self.assertEqual(name, "help")
        self.assertTrue(handler.await_args.args[1].layout_swapped)

    async def test_edit_updates_also_dispatch(self):
        handler, name = await self.exercise("p", "pping", "ping", source="edit")
        handler.assert_awaited_once()
        self.assertEqual(name, "ping")

    async def test_doubled_symbolic_prefixes_never_reach_handlers(self):
        for prefix in (".", "!", "::"):
            with self.subTest(prefix=prefix):
                handler, name = await self.exercise(prefix, prefix + prefix + "help", "help")
                handler.assert_not_awaited()
                self.assertIsNone(name)

    async def test_an_extra_letter_prefix_does_not_run_a_different_command(self):
        handler, name = await self.exercise("h", "hhhelp", "help")
        handler.assert_not_awaited()
        self.assertIsNone(name)

    async def test_registered_command_keeps_its_full_name_after_one_prefix(self):
        handler, name = await self.exercise("h", "hhhelp", "hhelp")
        handler.assert_awaited_once()
        self.assertEqual(name, "hhelp")
        self.assertEqual(handler.await_args.args[1].name, "hhelp")

    async def test_non_owner_still_cannot_execute_commands(self):
        for prefix, command in (("h", "help"), ("p", "ping"), (".", "help")):
            with self.subTest(prefix=prefix):
                handler, name = await self.exercise(prefix, prefix + command, command, actor=2, outgoing=False)
                handler.assert_not_awaited()
                self.assertEqual(name, command)

    async def test_prefix_command_updates_parser_without_restart(self):
        loader = SourceFileLoader("prefix_test_settings", str(ROOT / "constellations/settings.hmod"))
        spec = spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        settings = module_from_spec(spec)
        loader.exec_module(settings)
        values = {}
        config = RuntimeConfig(api_id=1, api_hash="test", bot_token=None, owner_id=1, prefix=".", session_name="test", session_dir=Path("."), state_path=Path("unused.sqlite3"), backup_keep=1)
        kernel = Kernel(parser=CommandParser("."))
        kernel.registry.register("help", lambda *args: None, module_id="prefix-test")
        kernel.registry.register("ping", lambda *args: None, module_id="prefix-test")
        runtime = SimpleNamespace(config=config, kernel=kernel, state=SimpleNamespace(set_setting=lambda key, value: values.__setitem__(key, value)))
        ctx = SimpleNamespace(runtime=runtime, respond=AsyncMock(), t=lambda key, **params: key)
        for prefix, text, name in (("h", "hhelp", "help"), ("p", "pping", "ping"), (".", ".help", "help")):
            with self.subTest(prefix=prefix):
                await settings.command_prefix(ctx, SimpleNamespace(args=(prefix,)))
                self.assertEqual(values["prefix"], prefix)
                self.assertEqual(runtime.config.prefix, prefix)
                self.assertEqual(kernel.parser.prefix, prefix)
                self.assertEqual(kernel.command_name(text), name)
        self.assertIsNone(kernel.command_name("..help"))


if __name__ == "__main__":
    unittest.main()
