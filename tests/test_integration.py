from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import web_to_obsidian as clip


ARTICLE = {
    "ok": True,
    "title": "An Article",
    "author": "Ada",
    "published": "2026-07-23",
    "description": "A useful page",
    "site": "Example",
    "canonicalUrl": "https://example.com/article",
    "url": "https://example.com/article",
    "keywords": ["security", "clipping"],
    "markdown": (
        "# An Article\n\n"
        + "Useful content for an integration test. " * 20
        + "\n\n![remote](https://cdn.example/image.png)\n"
    ),
    "wordCount": 140,
    "method": "static",
}


class ClipServiceIntegrationTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_service_writes_pushes_and_repeats_as_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            vault = root / "vault"
            lock_file = root / "vault.lock"

            self._git(root, "init", "--bare", str(remote))
            self._git(root, "init", "-b", "master", str(vault))
            self._git(vault, "config", "user.name", "Clip Test")
            self._git(vault, "config", "user.email", "clip@example.invalid")
            self._git(vault, "remote", "add", "origin", str(remote))
            (vault / "Inbox").mkdir()
            (vault / "Inbox" / ".gitkeep").write_text("", encoding="utf-8")
            self._git(vault, "add", "Inbox/.gitkeep")
            self._git(vault, "commit", "-m", "initial")
            self._git(vault, "push", "-u", "origin", "master")
            self._git(vault, "switch", "-c", "feature/web-to-obsidian-clip")
            self._git(vault, "push", "-u", "origin", "HEAD")

            env = {
                "WEB_TO_OBSIDIAN_VAULT": str(vault),
                "WEB_TO_OBSIDIAN_DEST": "Inbox",
                "WEB_TO_OBSIDIAN_IMAGES": "images",
                "WEB_TO_OBSIDIAN_SYNC_BRANCH": "feature/web-to-obsidian-clip",
                "WEB_TO_OBSIDIAN_LOCK_FILE": str(lock_file),
            }
            service = clip.ClipService(Path(__file__).parents[1], env=env)

            with mock.patch.object(clip, "run_extractor", return_value=dict(ARTICLE)):
                first = service.run("https://example.com/article")

            self.assertEqual(first.commit_state, "committed")
            self.assertEqual(first.push_state, "pushed")
            self.assertRegex(first.path, r"^Inbox/\d{4}-\d{2}-\d{2}-An Article\.md$")
            note = vault / first.path
            before_mtime = note.stat().st_mtime_ns
            before_head = self._git(vault, "rev-parse", "HEAD").stdout
            content = note.read_text(encoding="utf-8")
            frontmatter, _ = content[4:].split("\n---\n", 1)
            metadata = yaml.safe_load(frontmatter)
            self.assertEqual(metadata["source"], ARTICLE["canonicalUrl"])
            self.assertEqual(metadata["url"], ARTICLE["canonicalUrl"])
            self.assertEqual(metadata["keywords"], ARTICLE["keywords"])
            self.assertEqual(metadata["category"], "Inbox")
            self.assertEqual(metadata["extraction_method"], "static")
            self.assertIn("![remote](https://cdn.example/image.png)", content)

            remote_content = subprocess.run(
                [
                    "git",
                    f"--git-dir={remote}",
                    "show",
                    f"refs/heads/feature/web-to-obsidian-clip:{first.path}",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.decode("utf-8")
            self.assertEqual(remote_content, content)

            with mock.patch.object(clip, "run_extractor", return_value=dict(ARTICLE)):
                second = service.run("https://example.com/article")

            self.assertEqual(second.path, first.path)
            self.assertEqual(second.commit_state, "unchanged")
            self.assertEqual(second.push_state, "not_needed")
            self.assertEqual(note.stat().st_mtime_ns, before_mtime)
            self.assertEqual(self._git(vault, "rev-parse", "HEAD").stdout, before_head)
            self.assertEqual(
                self._git(vault, "status", "--porcelain=v1").stdout,
                b"",
            )


if __name__ == "__main__":
    unittest.main()
