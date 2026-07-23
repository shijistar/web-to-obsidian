import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

import web_to_obsidian as clip


ROOT = Path(__file__).resolve().parents[1]


class FakeContext:
    def __init__(self):
        self.calls = []

    def register_command(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class PluginRegistrationTests(unittest.TestCase):
    def _load_plugin(self):
        spec = importlib.util.spec_from_file_location(
            "web_to_obsidian_plugin",
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            return module
        finally:
            sys.modules.pop(spec.name, None)

    def test_registers_clip_with_documented_hint_and_closed_over_handler(self):
        plugin = self._load_plugin()
        context = FakeContext()
        plugin.register(context)

        self.assertEqual(len(context.calls), 1)
        args, kwargs = context.calls[0]
        self.assertEqual(args, ("clip",))
        self.assertEqual(
            kwargs["args_hint"],
            "<url> [--refresh] [--no-browser] [--no-git]",
        )
        self.assertIn("Obsidian", kwargs["description"])
        self.assertTrue(callable(kwargs["handler"]))

    def test_handler_never_propagates_and_does_not_expose_exception_details(self):
        with mock.patch.object(clip.ClipService, "run", side_effect=RuntimeError("SECRET stack")):
            response = clip.build_handler(ROOT)("https://example.com --no-git")
        self.assertIn("failed", response.lower())
        self.assertNotIn("SECRET", response)
        self.assertNotIn("RuntimeError", response)

    def test_git_preflight_happens_before_extractor(self):
        config = mock.Mock()
        options = clip.ClipOptions("https://example.com", False, False, False)
        config.vault = Path("/tmp/vault")
        config.sync_branch = "feature/web-to-obsidian-clip"
        config.lock_file = Path("/tmp/test-web-to-obsidian.lock")
        order = []
        with (
            mock.patch.object(clip, "parse_clip_args", return_value=options),
            mock.patch.object(clip.ClipConfig, "from_file", return_value=config),
            mock.patch.object(clip, "VaultLock", return_value=mock.MagicMock()),
            mock.patch.object(
                clip.GitSync,
                "preflight",
                side_effect=lambda vault, branch: order.append("preflight") or mock.Mock(),
            ),
            mock.patch.object(
                clip,
                "run_extractor",
                side_effect=lambda *a, **k: order.append("extractor") or {},
            ),
            mock.patch.object(clip, "render_note", side_effect=clip.ClipError("stop")),
        ):
            with self.assertRaises(clip.ClipError):
                clip.ClipService(ROOT).run("https://example.com")
        self.assertEqual(order, ["preflight", "extractor"])


if __name__ == "__main__":
    unittest.main()
