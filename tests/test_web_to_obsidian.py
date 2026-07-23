import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import web_to_obsidian as clip


SUCCESS = {
    "ok": True,
    "title": "An Article",
    "author": "Ada",
    "published": "2026-07-23",
    "description": "A useful page",
    "site": "Example",
    "canonicalUrl": "https://example.com/article",
    "url": "https://example.com/article",
    "keywords": ["security", "clipping"],
    "markdown": "# An Article\r\n\r\nBody\r\n",
    "wordCount": 3,
    "method": "readability",
}


class ArgsTests(unittest.TestCase):
    def test_accepts_one_http_url_and_supported_flags(self):
        parsed = clip.parse_clip_args(
            '"https://example.com/a?x=1&y=2" --no-browser --no-git'
        )
        self.assertEqual(parsed.url, "https://example.com/a?x=1&y=2")
        self.assertTrue(parsed.no_browser)
        self.assertTrue(parsed.no_git)

    def test_rejects_unknown_option_extra_url_and_non_http_url(self):
        invalid = (
            "https://example.com --wat",
            "https://example.com https://other.example",
            "file:///etc/passwd",
            "",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(clip.ClipError):
                clip.parse_clip_args(raw)

    def test_rejects_malformed_shell_quoting(self):
        with self.assertRaises(clip.ClipError):
            clip.parse_clip_args("'https://example.com")


class ConfigAndFilenameTests(unittest.TestCase):
    def test_destination_must_resolve_inside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            with self.assertRaises(clip.ClipError):
                clip.ClipConfig.from_env(
                    {
                        "WEB_TO_OBSIDIAN_VAULT": str(vault),
                        "WEB_TO_OBSIDIAN_DEST": "../escape",
                    }
                )

    def test_toml_config_loads_and_lock_must_remain_outside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                "[clip]\n"
                f'vault = "{vault}"\n'
                'destination = "Inbox"\n'
                'images = "images"\n'
                'sync_branch = "feature/web-to-obsidian-clip"\n'
                f'lock_file = "{root / "vault.lock"}"\n',
                encoding="utf-8",
            )
            config = clip.ClipConfig.from_file(config_path)
            self.assertEqual(config.destination, vault / "Inbox")
            self.assertEqual(config.sync_branch, "feature/web-to-obsidian-clip")

            with self.assertRaisesRegex(clip.ClipError, "lock file"):
                clip.ClipConfig.from_env(
                    {
                        "WEB_TO_OBSIDIAN_VAULT": str(vault),
                        "WEB_TO_OBSIDIAN_LOCK_FILE": str(vault / "bad.lock"),
                    }
                )

    def test_default_destination_is_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            config = clip.ClipConfig.from_env({"WEB_TO_OBSIDIAN_VAULT": str(vault)})
            self.assertEqual(config.destination, vault / "Inbox")

    def test_filename_removes_traversal_controls_and_forbidden_characters(self):
        name = clip.safe_filename("  ../A:<B>\\C/\x00\n...  ", "https://example.com/x")
        self.assertTrue(name.endswith(".md"))
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn("..", name)
        self.assertNotIn(":", name)
        self.assertNotIn("\x00", name)

    def test_filename_avoids_reserved_dos_basename(self):
        for title in ("CON", "con.txt", "LPT1", "aux"):
            with self.subTest(title=title):
                name = clip.safe_filename(title, "https://example.com/reserved")
                self.assertFalse(name.removesuffix(".md").split(".", 1)[0].upper() in clip.DOS_RESERVED)

    def test_filename_caps_utf8_without_splitting_unicode(self):
        name = clip.safe_filename("界" * 200, "https://example.com/unicode", max_bytes=47)
        name.encode("utf-8")
        self.assertLessEqual(len(name.encode("utf-8")), 47)
        self.assertTrue(name.endswith(".md"))

    def test_empty_sanitized_title_uses_hash_fallback(self):
        first = clip.safe_filename("////", "https://example.com/fallback")
        second = clip.safe_filename("////", "https://example.com/fallback")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{12}\.md$")


class RenderingTests(unittest.TestCase):
    def test_yaml_quotes_injection_strings_and_normalizes_line_endings(self):
        data = dict(SUCCESS)
        data["title"] = 'Title"\n---\nevil: true'
        data["description"] = "line one\r\nline two"
        note = clip.render_note(data, created="2026-07-23T12:00:00+00:00")

        self.assertNotIn("\r", note)
        self.assertTrue(note.startswith("---\n"))
        frontmatter, _ = note[4:].split("\n---\n", 1)
        parsed = yaml.safe_load(frontmatter)
        self.assertEqual(parsed["title"], data["title"])
        self.assertEqual(parsed["source"], "https://example.com/article")
        self.assertEqual(parsed["url"], "https://example.com/article")
        self.assertEqual(parsed["keywords"], ["security", "clipping"])
        self.assertEqual(parsed["category"], "Inbox")
        self.assertEqual(parsed["created"], "2026-07-23T12:00:00+00:00")
        self.assertEqual(parsed["extraction_method"], "readability")
        self.assertEqual(parsed["tags"], ["web-clip"])
        self.assertEqual(note.count("\n---\n"), 1)

    def test_render_note_accepts_prevalidated_extractor_payload_without_ok_flag(self):
        validated = clip._validate_success_payload(SUCCESS)
        note = clip.render_note(validated, created="2026-07-23T12:00:00+00:00")
        self.assertIn("title: An Article", note)
        self.assertIn("keywords:", note)


class TargetAndAtomicWriteTests(unittest.TestCase):
    def _note(self, source, title="Old"):
        data = dict(SUCCESS, title=title, canonicalUrl=source, url=source)
        return clip.render_note(data, created="2026-07-23T12:00:00+00:00")

    def test_same_normalized_source_overwrites_same_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            target = destination / clip.safe_filename("An Article", SUCCESS["canonicalUrl"])
            target.write_text(self._note("HTTPS://EXAMPLE.COM:443/article#old"), encoding="utf-8")

            chosen = clip.choose_target(
                destination, "An Article", "https://example.com/article"
            )
            self.assertEqual(chosen, target)

    def test_new_target_uses_capture_date_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            chosen = clip.choose_target(
                destination,
                "An Article",
                "https://example.com/article",
                capture_date="2026-07-23",
            )
            self.assertEqual(chosen.name, "2026-07-23-An Article.md")

    def test_same_source_on_a_later_date_reuses_existing_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            existing = destination / "2026-07-22-An Article.md"
            existing.write_text(
                self._note("https://example.com/article"), encoding="utf-8"
            )

            chosen = clip.choose_target(
                destination,
                "Renamed Article",
                "https://example.com/article",
                capture_date="2026-07-23",
            )
            self.assertEqual(chosen, existing)

    def test_multiple_existing_notes_for_same_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            for day in ("2026-07-21", "2026-07-22"):
                (destination / f"{day}-An Article.md").write_text(
                    self._note("https://example.com/article"), encoding="utf-8"
                )

            with self.assertRaisesRegex(clip.ClipError, "multiple"):
                clip.choose_target(
                    destination,
                    "An Article",
                    "https://example.com/article",
                    capture_date="2026-07-23",
                )

    def test_title_collision_with_different_source_adds_url_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            base = destination / clip.safe_filename(
                "2026-07-23-An Article", "https://one.example"
            )
            base.write_text(self._note("https://one.example/"), encoding="utf-8")

            chosen1 = clip.choose_target(
                destination,
                "An Article",
                "https://two.example",
                capture_date="2026-07-23",
            )
            chosen2 = clip.choose_target(
                destination,
                "An Article",
                "https://two.example",
                capture_date="2026-07-23",
            )
            self.assertEqual(chosen1, chosen2)
            self.assertNotEqual(chosen1, base)
            self.assertRegex(
                chosen1.name, r"^2026-07-23-An Article-[0-9a-f]{8}\.md$"
            )
            self.assertEqual(chosen1.parent, destination)

    def test_existing_symlink_escaping_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "dest"
            destination.mkdir()
            outside = root / "outside.md"
            outside.write_text(self._note("https://example.com/article"), encoding="utf-8")
            target = destination / clip.safe_filename("An Article", SUCCESS["canonicalUrl"])
            target.symlink_to(outside)
            with self.assertRaises(clip.ClipError):
                clip.choose_target(destination, "An Article", SUCCESS["canonicalUrl"])

    def test_atomic_write_replaces_target_and_uses_same_directory_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.md"
            target.write_text("old", encoding="utf-8")
            real_replace = os.replace
            calls = []

            def recording_replace(source, destination):
                calls.append((Path(source), Path(destination)))
                return real_replace(source, destination)

            with mock.patch.object(clip.os, "replace", side_effect=recording_replace):
                clip.atomic_write(target, "new\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0].parent, target.parent)
            self.assertEqual(calls[0][1], target)
            self.assertFalse(any(p.name.startswith(".clip-") for p in target.parent.iterdir()))


class ExtractorTests(unittest.TestCase):
    def test_node_command_is_a_list_and_never_uses_shell(self):
        process = mock.Mock()
        process.stdout = io.BytesIO(json.dumps(SUCCESS).encode())
        process.stderr = io.BytesIO(b"")
        process.wait.return_value = 0
        process.poll.return_value = 0

        with mock.patch.object(clip.subprocess, "Popen", return_value=process) as popen:
            result = clip.run_extractor(
                Path("/plugins/web-to-obsidian"),
                "https://example.com/article",
                no_browser=True,
            )

        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(
            args[0],
            [
                "node",
                "/plugins/web-to-obsidian/extractor/src/cli.mjs",
                "https://example.com/article",
                "--no-browser",
            ],
        )
        self.assertIs(kwargs["shell"], False)
        self.assertLessEqual(kwargs.get("timeout", clip.EXTRACTOR_TIMEOUT), 120)
        self.assertEqual(result["title"], "An Article")

    def test_malformed_or_oversized_extractor_json_is_rejected_without_stderr(self):
        malformed = clip.ProcessResult(0, b"not json", b"SECRET traceback")
        with mock.patch.object(clip, "_run_bounded", return_value=malformed):
            with self.assertRaises(clip.ClipError) as caught:
                clip.run_extractor(Path("/plugin"), "https://example.com")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("traceback", str(caught.exception).lower())

    def test_extractor_success_payload_requires_all_typed_fields(self):
        bad = dict(SUCCESS, wordCount="three")
        result = clip.ProcessResult(0, json.dumps(bad).encode(), b"")
        with mock.patch.object(clip, "_run_bounded", return_value=result):
            with self.assertRaises(clip.ClipError):
                clip.run_extractor(Path("/plugin"), "https://example.com")

    def test_extractor_success_payload_requires_keywords_to_be_a_string_list(self):
        bad = dict(SUCCESS, keywords=["ok", 1])
        result = clip.ProcessResult(0, json.dumps(bad).encode(), b"")
        with mock.patch.object(clip, "_run_bounded", return_value=result):
            with self.assertRaises(clip.ClipError):
                clip.run_extractor(Path("/plugin"), "https://example.com")


class GitSafetyTests(unittest.TestCase):
    def _git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_refuses_dirty_worktree_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-b", "feature/clip")
            (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaises(clip.ClipError) as caught:
                clip.GitSync.preflight(repo)
            self.assertIn("clean", str(caught.exception).lower())

    def test_refuses_protected_core_branches(self):
        for branch in ("main", "master", "dev", "develop"):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._git(repo, "init", "-b", branch)
                with self.assertRaises(clip.ClipError) as caught:
                    clip.GitSync.preflight(repo)
                self.assertIn("protected", str(caught.exception).lower())

    def test_refuses_cherry_pick_and_revert_states(self):
        for state_name in ("CHERRY_PICK_HEAD", "REVERT_HEAD"):
            with self.subTest(state=state_name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._git(repo, "init", "-b", "feature/clip")
                self._git(repo, "config", "user.name", "Clip Test")
                self._git(repo, "config", "user.email", "clip@example.invalid")
                (repo / ".gitkeep").write_text("", encoding="utf-8")
                self._git(repo, "add", ".gitkeep")
                self._git(repo, "commit", "-m", "initial")
                state_path = Path(
                    self._git(repo, "rev-parse", "--git-path", state_name)
                    .stdout.decode()
                    .strip()
                )
                if not state_path.is_absolute():
                    state_path = repo / state_path
                state_path.write_text(
                    self._git(repo, "rev-parse", "HEAD").stdout.decode(),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(clip.ClipError, "operation"):
                    clip.GitSync.preflight(repo)

    def test_stages_only_generated_path_commits_and_pushes_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "vault"
            self._git(root, "init", "--bare", str(remote))
            self._git(root, "init", "-b", "feature/clip", str(repo))
            self._git(repo, "config", "user.name", "Clip Test")
            self._git(repo, "config", "user.email", "clip@example.invalid")
            self._git(repo, "remote", "add", "origin", str(remote))
            (repo / ".gitkeep").write_text("", encoding="utf-8")
            self._git(repo, "add", ".gitkeep")
            self._git(repo, "commit", "-m", "initial")

            sync = clip.GitSync.preflight(repo)
            dest = repo / "Unclassified"
            dest.mkdir()
            note = dest / "Article.md"
            note.write_text("body\n", encoding="utf-8")
            outcome = sync.finalize([note])

            self.assertEqual(outcome.commit_state, "committed")
            self.assertEqual(outcome.push_state, "pushed")
            changed = self._git(repo, "show", "--pretty=", "--name-only", "HEAD").stdout
            self.assertEqual(changed.decode().strip(), "Unclassified/Article.md")
            upstream = self._git(repo, "rev-parse", "--abbrev-ref", "@{upstream}").stdout
            self.assertEqual(upstream.decode().strip(), "origin/feature/clip")

    def test_post_commit_verification_blocks_hook_added_paths_from_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "vault"
            self._git(root, "init", "--bare", str(remote))
            self._git(root, "init", "-b", "feature/clip", str(repo))
            self._git(repo, "config", "user.name", "Clip Test")
            self._git(repo, "config", "user.email", "clip@example.invalid")
            self._git(repo, "remote", "add", "origin", str(remote))
            (repo / ".gitkeep").write_text("", encoding="utf-8")
            self._git(repo, "add", ".gitkeep")
            self._git(repo, "commit", "-m", "initial")

            hook_path = repo / ".git" / "hooks" / "pre-commit"
            hook_path.write_text(
                "#!/bin/sh\n"
                "printf 'hook data\\n' > hook-added.txt\n"
                "git add -- hook-added.txt\n",
                encoding="utf-8",
            )
            hook_path.chmod(0o700)

            sync = clip.GitSync.preflight(repo)
            destination = repo / "Unclassified"
            destination.mkdir()
            note = destination / "Article.md"
            note.write_text("body\n", encoding="utf-8")
            outcome = sync.finalize([note])

            self.assertEqual(outcome.commit_state, "committed_unverified")
            self.assertEqual(outcome.push_state, "not_attempted")
            remote_branch = subprocess.run(
                [
                    "git",
                    f"--git-dir={remote}",
                    "show-ref",
                    "--verify",
                    "refs/heads/feature/clip",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(remote_branch.returncode, 0)


if __name__ == "__main__":
    unittest.main()
