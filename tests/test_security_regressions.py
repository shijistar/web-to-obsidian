import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest import mock

import yaml

import web_to_obsidian as clip


SUCCESS = {
    "ok": True,
    "title": 'Title: "quoted"',
    "author": "Ada",
    "published": "2026-07-23",
    "description": "Description",
    "site": "Example",
    "canonicalUrl": "https://example.com/article",
    "url": "https://example.com/article",
    "keywords": ["security", "clipping"],
    "markdown": "# Article\n\n" + ("body " * 60),
    "wordCount": 61,
    "method": "static",
}


class ExtractorIsolationRegressionTests(unittest.TestCase):
    def test_extractor_receives_allowlisted_environment_without_secret_tokens(self):
        process = mock.Mock()
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()
        process.stdout.read.side_effect = [json.dumps(SUCCESS).encode(), b""]
        process.stderr.read.side_effect = [b""]
        process.wait.return_value = 0
        process.pid = 12345

        env = {
            "HOME": "/home/test",
            "PATH": "/safe/bin",
            "LANG": "C.UTF-8",
            "GH_TOKEN": "secret",
            "OPENAI_API_KEY": "secret",
            "NODE_OPTIONS": "--require=/tmp/evil.js",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(clip.subprocess, "Popen", return_value=process) as popen,
        ):
            clip.run_extractor(Path("/plugin"), SUCCESS["url"])

        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env["HOME"], "/home/test")
        self.assertEqual(child_env["PATH"], "/safe/bin")
        self.assertNotIn("GH_TOKEN", child_env)
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("NODE_OPTIONS", child_env)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    @unittest.skipUnless(Path("/proc").is_dir(), "Linux process checks require /proc")
    def test_timeout_terminates_the_complete_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            code = (
                "import subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"open({str(pid_file)!r},'w').write(str(p.pid)); "
                "time.sleep(60)"
            )
            started = time.monotonic()
            with self.assertRaises(clip.ClipError):
                clip._run_bounded(
                    [os.sys.executable, "-c", code],
                    timeout=1,
                    stdout_limit=1024,
                    stderr_limit=1024,
                )
            self.assertLess(time.monotonic() - started, 5)
            grandchild = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while Path(f"/proc/{grandchild}").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(Path(f"/proc/{grandchild}").exists())


class VaultIdempotencyRegressionTests(unittest.TestCase):
    def test_frontmatter_is_yaml_serialized_and_contains_stable_hashes(self):
        note = clip.render_note(SUCCESS, created="2026-07-23T12:00:00+00:00")
        _, frontmatter, _ = note.split("---\n", 2)
        parsed = yaml.safe_load(frontmatter)
        self.assertEqual(parsed["title"], SUCCESS["title"])
        self.assertRegex(parsed["webclip_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(parsed["content_hash"], r"^sha256:[0-9a-f]{64}$")

    def test_active_markdown_content_is_neutralized_but_https_images_remain(self):
        data = dict(SUCCESS)
        data["markdown"] = (
            "<!-- webclip:manual:start -->\n"
            "<iframe src=\"file:///etc/passwd\">hidden</iframe>\n"
            "<a href=\"file:///etc/passwd\">raw local</a>\n"
            "[local](file:///etc/passwd)\n"
            "[code](javascript:alert(1))\n"
            "![[Private Note]]\n"
            "![remote](https://cdn.example/image.png)\n"
        )
        note = clip.render_note(data, created="2026-07-23T00:00:00+00:00")
        self.assertNotIn("<iframe", note.lower())
        self.assertNotIn("<!-- webclip:manual:start -->\n<!-- webclip:manual:start -->", note)
        self.assertNotIn("](file:", note.lower())
        self.assertNotIn("](javascript:", note.lower())
        self.assertNotIn("![[", note)
        self.assertIn("![remote](https://cdn.example/image.png)", note)
        self.assertIn("&lt;a href=\"file:///etc/passwd\">", note)

    def test_same_content_is_noop_and_changed_content_requires_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "article.md"
            first = clip.render_note(SUCCESS, created="2026-07-23T12:00:00+00:00")
            self.assertEqual(clip.write_managed_note(target, first, refresh=False), "written")
            before = target.stat().st_mtime_ns
            self.assertEqual(clip.write_managed_note(target, first, refresh=False), "unchanged")
            self.assertEqual(target.stat().st_mtime_ns, before)

            changed_data = dict(SUCCESS, markdown=SUCCESS["markdown"] + "changed\n")
            changed = clip.render_note(changed_data, created="2026-07-23T13:00:00+00:00")
            with self.assertRaisesRegex(clip.ClipError, "--refresh"):
                clip.write_managed_note(target, changed, refresh=False)
            self.assertEqual(target.read_text(encoding="utf-8"), first)
            self.assertEqual(clip.write_managed_note(target, changed, refresh=True), "written")
            self.assertEqual(target.read_text(encoding="utf-8"), changed)


class SharedLockRegressionTests(unittest.TestCase):
    def test_second_lock_contender_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vault.lock"
            with clip.VaultLock(path):
                with self.assertRaises(clip.ClipError):
                    with clip.VaultLock(path):
                        self.fail("second contender unexpectedly acquired the lock")


if __name__ == "__main__":
    unittest.main()
