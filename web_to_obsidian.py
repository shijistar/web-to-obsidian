"""Safe vault-writing and Git synchronization for the ``/clip`` command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import tomllib
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from typing import Mapping, Sequence

import yaml


EXTRACTOR_TIMEOUT = 90
GIT_TIMEOUT = 120
MAX_STDOUT_BYTES = 12 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FILENAME_BYTES = 180
MAX_DESTINATION_SCAN_ENTRIES = 10_000
DOS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_PROTECTED_BRANCHES = {"main", "master", "dev", "develop"}
_FORBIDDEN_FILENAME = set('<>:"/\\|?*')
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ClipError(Exception):
    """An expected error whose message is safe to show to the user."""


@dataclass(frozen=True)
class ClipOptions:
    url: str
    no_browser: bool
    no_git: bool
    refresh: bool


@dataclass(frozen=True)
class ClipConfig:
    vault: Path
    destination: Path
    images: Path
    sync_branch: str
    lock_file: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ClipConfig":
        values = os.environ if env is None else env
        vault = Path(
            values.get("WEB_TO_OBSIDIAN_VAULT", "~/obsidian/shijistar")
        ).expanduser().resolve()
        if not vault.is_dir():
            raise ClipError("The configured Obsidian vault does not exist.")

        destination = _resolve_vault_path(
            vault, values.get("WEB_TO_OBSIDIAN_DEST", "Inbox")
        )
        images = _resolve_vault_path(
            vault, values.get("WEB_TO_OBSIDIAN_IMAGES", "images")
        )
        sync_branch = values.get(
            "WEB_TO_OBSIDIAN_SYNC_BRANCH", "feature/web-to-obsidian-clip"
        ).strip()
        if not sync_branch or sync_branch.lower() in _PROTECTED_BRANCHES:
            raise ClipError("The configured Git sync branch is unsafe.")
        default_lock = (
            Path("~/.local/state/web-to-obsidian").expanduser()
            / f"{hashlib.sha256(str(vault).encode('utf-8')).hexdigest()[:16]}.lock"
        )
        lock_file = Path(
            values.get("WEB_TO_OBSIDIAN_LOCK_FILE", str(default_lock))
        ).expanduser().resolve()
        if lock_file == vault or vault in lock_file.parents:
            raise ClipError("The shared lock file must be outside the Obsidian vault.")
        return cls(
            vault=vault,
            destination=destination,
            images=images,
            sync_branch=sync_branch,
            lock_file=lock_file,
        )

    @classmethod
    def from_file(cls, path: Path) -> "ClipConfig":
        if not path.is_file():
            return cls.from_env()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ClipError("The plugin config.toml is invalid.") from exc
        section = data.get("clip")
        if not isinstance(section, dict):
            raise ClipError("The plugin config.toml must contain a [clip] table.")
        supported = {"vault", "destination", "images", "sync_branch", "lock_file"}
        if set(section) - supported or any(
            not isinstance(value, str) for value in section.values()
        ):
            raise ClipError("The plugin config.toml contains unsupported values.")
        mapping = {
            "WEB_TO_OBSIDIAN_VAULT": section.get("vault", "~/obsidian/shijistar"),
            "WEB_TO_OBSIDIAN_DEST": section.get("destination", "Inbox"),
            "WEB_TO_OBSIDIAN_IMAGES": section.get("images", "images"),
            "WEB_TO_OBSIDIAN_SYNC_BRANCH": section.get(
                "sync_branch", "feature/web-to-obsidian-clip"
            ),
        }
        if "lock_file" in section:
            mapping["WEB_TO_OBSIDIAN_LOCK_FILE"] = section["lock_file"]
        return cls.from_env(mapping)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class GitOutcome:
    commit_state: str
    push_state: str
    detail: str = ""


@dataclass(frozen=True)
class ClipResult:
    path: str
    commit_state: str
    push_state: str

    def user_message(self) -> str:
        if self.commit_state == "disabled":
            return f"Saved clip: {self.path} (Git synchronization disabled)."
        if self.commit_state == "committed" and self.push_state == "pushed":
            return f"Saved clip: {self.path} (committed and pushed)."
        if self.commit_state == "unchanged":
            return f"Saved clip: {self.path} (content unchanged; no Git commit needed)."
        if self.commit_state == "committed":
            return f"Saved clip: {self.path} (committed; push failed)."
        if self.commit_state == "committed_unverified":
            return (
                f"Saved clip: {self.path} "
                "(local commit created, but post-commit verification failed; not pushed)."
            )
        if self.commit_state == "commit_failed":
            return f"Saved clip: {self.path} (Git commit failed; not pushed)."
        if self.commit_state == "stage_failed":
            return f"Saved clip: {self.path} (Git staging failed; not committed or pushed)."
        return (
            f"Saved clip: {self.path} "
            "(Git safety verification refused synchronization; not committed or pushed)."
        )


def _resolve_vault_path(vault: Path, configured: str) -> Path:
    raw = Path(configured).expanduser()
    candidate = raw if raw.is_absolute() else vault / raw
    resolved = candidate.resolve()
    _require_within(resolved, vault, "Configured path must remain inside the vault.")
    return resolved


def _require_within(path: Path, parent: Path, message: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise ClipError(message) from exc


def _validate_http_url(value: str) -> None:
    if len(value) > 8192:
        raise ClipError("The URL is too long.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ClipError("A valid HTTP or HTTPS URL is required.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not 0 < port < 65536
    ):
        raise ClipError("A valid HTTP or HTTPS URL is required.")


def parse_clip_args(raw_args: str) -> ClipOptions:
    """Parse one URL and the two supported flags without invoking a shell."""
    try:
        tokens = shlex.split(raw_args, posix=True)
    except ValueError as exc:
        raise ClipError("Invalid quoting in /clip arguments.") from exc

    no_browser = False
    no_git = False
    refresh = False
    urls: list[str] = []
    for token in tokens:
        if token == "--no-browser":
            no_browser = True
        elif token == "--no-git":
            no_git = True
        elif token == "--refresh":
            refresh = True
        elif token.startswith("-"):
            raise ClipError("Unknown /clip option.")
        else:
            urls.append(token)
    if len(urls) != 1:
        raise ClipError(
            "Usage: /clip <url> [--refresh] [--no-browser] [--no-git]"
        )
    _validate_http_url(urls[0])
    return ClipOptions(
        url=urls[0], no_browser=no_browser, no_git=no_git, refresh=refresh
    )


def normalize_url(value: str) -> str:
    """Return a stable URL form used only for source identity comparisons."""
    _validate_http_url(value)
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ClipError("The page returned an invalid source URL.") from exc
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _content_hash(markdown: str) -> str:
    normalized = _normalize_text(markdown).rstrip("\n") + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_DANGEROUS_SCHEMES = r"(?:javascript|vbscript|file|obsidian|data)"


def sanitize_markdown(markdown: str) -> str:
    text = _normalize_text(markdown)
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    dangerous_tags = r"script|iframe|object|embed|form|input|button|textarea|select|option|style|link|meta|base|svg|math"
    text = re.sub(
        rf"(?is)<(?P<tag>{dangerous_tags})\b[^>]*>.*?</(?P=tag)\s*>",
        "",
        text,
    )
    text = re.sub(rf"(?is)</?(?:{dangerous_tags})\b[^>]*>", "", text)
    text = re.sub(r"(?i)<(?=/?[A-Za-z][A-Za-z0-9-]*(?:\s|/?>))", "&lt;", text)
    text = re.sub(
        rf"(?i)(\]\(\s*<?){_DANGEROUS_SCHEMES}:",
        r"\1blocked:",
        text,
    )
    text = re.sub(
        rf"(?im)^(\s*\[[^\]\n]+\]:\s*<?){_DANGEROUS_SCHEMES}:",
        r"\1blocked:",
        text,
    )
    return text.replace("[[", r"\[\[")


def _contains_markdown_h1(markdown: str) -> bool:
    in_fence = False
    fence_char = ""
    fence_length = 0
    previous_text_line = None
    for raw_line in _normalize_text(markdown).split("\n"):
        if raw_line.startswith("\t"):
            previous_text_line = None
            continue

        leading_spaces = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line[leading_spaces:]

        if leading_spaces <= 3:
            fence_match = re.match(r"(`{3,}|~{3,})", content)
            if fence_match:
                marker = fence_match.group(1)
                if not in_fence:
                    in_fence = True
                    fence_char = marker[0]
                    fence_length = len(marker)
                elif marker[0] == fence_char and len(marker) >= fence_length:
                    in_fence = False
                    fence_char = ""
                    fence_length = 0
                previous_text_line = None
                continue

        if in_fence:
            continue

        if leading_spaces >= 4:
            previous_text_line = None
            continue

        if re.match(r"^#(?:\s+\S|\s*$)", content):
            return True
        if re.match(r"^=+\s*$", content) and previous_text_line:
            return True

        previous_text_line = content if content.strip() else None
    return False


def _sanitized_heading_title(title: str) -> str:
    sanitized = sanitize_markdown(title)
    return re.sub(r"\s+", " ", sanitized).strip()


def _managed_markdown(title: str, markdown: str) -> str:
    cleaned_title = _sanitized_heading_title(title)
    cleaned_markdown = _normalize_text(markdown).rstrip("\n")
    if _contains_markdown_h1(cleaned_markdown):
        return cleaned_markdown
    if not cleaned_markdown:
        return f"# {cleaned_title}"
    return f"# {cleaned_title}\n\n{cleaned_markdown}"


def render_note(data: Mapping[str, object], created: str | None = None) -> str:
    """Render extractor data as normalized Markdown with YAML frontmatter."""
    checked = _validate_success_payload(data)
    timestamp = created or datetime.now(timezone.utc).isoformat(timespec="seconds")
    source = normalize_url(str(checked["canonicalUrl"] or checked["url"]))
    fetched_url = normalize_url(str(checked["url"]))
    markdown = sanitize_markdown(str(checked["markdown"])).rstrip("\n")
    managed_markdown = _managed_markdown(str(checked["title"]), markdown)
    metadata = {
        "title": checked["title"],
        "url": source,
        "author": checked["author"],
        "site": checked["site"],
        "description": checked["description"],
        "keywords": checked["keywords"],
        "tags": ["web-clip"],
        "original_url": source,
        "original_host": urlsplit(source).hostname or "",
        **({"fetched_url": fetched_url} if fetched_url != source else {}),
        "extraction_method": checked["method"],
        "status": "needs-review",
        "category": "Inbox",
        "word_count": checked["wordCount"],
        "webclip_id": "sha256:" + _url_hash(source),
        "content_hash": "sha256:" + _content_hash(markdown),
        "published": checked["published"],
        "created": timestamp,
    }
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip("\n")
    return (
        f"---\n{frontmatter}\n---\n\n"
        "<!-- webclip:managed:start -->\n"
        f"{managed_markdown}\n"
        "<!-- webclip:managed:end -->\n\n"
        "<!-- webclip:manual:start -->\n"
        "<!-- webclip:manual:end -->\n"
    )


def _url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def _truncate_utf8(value: str, byte_limit: int) -> str:
    used = 0
    result: list[str] = []
    for char in value:
        encoded_size = len(char.encode("utf-8"))
        if used + encoded_size > byte_limit:
            break
        result.append(char)
        used += encoded_size
    return "".join(result)


def safe_filename(title: str, url: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    """Build a portable Unicode Markdown filename capped by encoded byte size."""
    if max_bytes < 16:
        raise ClipError("The configured filename limit is too small.")
    cleaned_chars = []
    for char in _normalize_text(title):
        if char in _FORBIDDEN_FILENAME or unicodedata.category(char).startswith("C"):
            continue
        cleaned_chars.append(char)
    stem = "".join(cleaned_chars)
    stem = re.sub(r"\s+", " ", stem)
    stem = re.sub(r"\.+", ".", stem).strip(" .")
    if not stem:
        stem = _url_hash(url)[:12]
    if stem.split(".", 1)[0].upper() in DOS_RESERVED:
        stem = f"_{stem}"

    extension = ".md"
    stem = _truncate_utf8(stem, max_bytes - len(extension)).rstrip(" .")
    if not stem:
        stem = _truncate_utf8(_url_hash(url)[:12], max_bytes - len(extension))
    return stem + extension


def _filename_with_suffix(title: str, url: str, suffix: str) -> str:
    base = safe_filename(title, url, MAX_FILENAME_BYTES).removesuffix(".md")
    tail = f"-{suffix}.md"
    base = _truncate_utf8(base, MAX_FILENAME_BYTES - len(tail)).rstrip(" .")
    if not base:
        base = _url_hash(url)[:12]
    return base + tail


def _frontmatter_from_text(text: str) -> dict[str, object] | None:
    normalized = _normalize_text(text)
    if not normalized.startswith("---\n"):
        return None
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        parsed = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _source_from_note(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return None
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    metadata = _frontmatter_from_text(text)
    source = None
    if metadata is not None:
        for field in ("url", "original_url", "source"):
            candidate = metadata.get(field)
            if isinstance(candidate, str) and candidate:
                source = candidate
                break
    try:
        return normalize_url(source) if isinstance(source, str) else None
    except ClipError:
        return None


def _checked_candidate(destination: Path, filename: str) -> Path:
    candidate = destination / filename
    if candidate.is_symlink():
        raise ClipError("Refusing to replace a symbolic-link note.")
    resolved = candidate.resolve()
    _require_within(resolved, destination, "Generated note path escaped its destination.")
    return candidate


def choose_target(
    destination: Path,
    title: str,
    source: str,
    *,
    capture_date: str | None = None,
) -> Path:
    """Choose an idempotent target without scanning outside the destination."""
    destination = destination.resolve()
    if not destination.is_dir():
        raise ClipError("The configured clip destination is unavailable.")
    normalized_source = normalize_url(source)
    entries: list[Path] = []
    for index, existing in enumerate(destination.iterdir(), start=1):
        if index > MAX_DESTINATION_SCAN_ENTRIES:
            raise ClipError("The clip destination contains too many entries to scan safely.")
        entries.append(existing)

    matches: list[Path] = []
    for existing in sorted(entries, key=lambda path: path.name):
        if existing.suffix.lower() != ".md":
            continue
        if existing.is_symlink():
            raise ClipError("Refusing to scan a symbolic-link note.")
        if existing.is_file() and _source_from_note(existing) == normalized_source:
            matches.append(existing)
    if len(matches) > 1:
        raise ClipError("The clip destination contains multiple notes for this source.")
    if matches:
        return matches[0]

    if capture_date is None:
        capture_date = datetime.now(timezone.utc).date().isoformat()
    try:
        parsed_date = datetime.strptime(capture_date, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ClipError("The capture date is invalid.") from exc
    if parsed_date != capture_date:
        raise ClipError("The capture date is invalid.")

    dated_title = f"{capture_date}-{title}"
    base = _checked_candidate(destination, safe_filename(dated_title, source))
    if not base.exists() or _source_from_note(base) == normalized_source:
        return base

    digest = _url_hash(source)
    for length in range(8, len(digest) + 1, 4):
        candidate = _checked_candidate(
            destination, _filename_with_suffix(dated_title, source, digest[:length])
        )
        if not candidate.exists() or _source_from_note(candidate) == normalized_source:
            return candidate
    raise ClipError("Unable to choose a unique filename for the clip.")


def atomic_write(target: Path, content: str) -> None:
    """Durably replace a note using a temporary file in the same directory."""
    parent = target.parent.resolve()
    if not parent.is_dir():
        raise ClipError("The configured clip destination is unavailable.")
    if target.is_symlink():
        raise ClipError("Refusing to replace a symbolic-link note.")
    _require_within(target.resolve(), parent, "Generated note path escaped its destination.")

    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".clip-", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(_normalize_text(content))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except ClipError:
        raise
    except OSError as exc:
        raise ClipError("Could not safely write the clipped note.") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _manual_section(text: str) -> str:
    start_marker = "<!-- webclip:manual:start -->\n"
    end_marker = "<!-- webclip:manual:end -->"
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start)
    return text[start:end] if end >= 0 else ""


def _managed_section(text: str) -> str | None:
    start_marker = "<!-- webclip:managed:start -->\n"
    end_marker = "<!-- webclip:managed:end -->"
    start = text.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    end = text.find(end_marker, start)
    return text[start:end] if end >= 0 else None


def _note_semantic_state(text: str) -> tuple[dict[str, object], str] | None:
    metadata = _frontmatter_from_text(text)
    managed = _managed_section(text)
    if metadata is None or managed is None:
        return None
    comparable_metadata = dict(metadata)
    comparable_metadata.pop("created", None)
    return comparable_metadata, managed


def _with_manual_section(note: str, manual: str) -> str:
    marker = "<!-- webclip:manual:start -->\n"
    start = note.find(marker)
    end_marker = "<!-- webclip:manual:end -->"
    end = note.find(end_marker, start + len(marker)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ClipError("The generated note is missing managed boundaries.")
    return note[: start + len(marker)] + manual + note[end:]


def write_managed_note(target: Path, content: str, *, refresh: bool) -> str:
    """Write a new managed note, no-op identical content, or require refresh."""
    if not target.exists():
        atomic_write(target, content)
        return "written"
    if target.is_symlink():
        raise ClipError("Refusing to replace a symbolic-link note.")
    try:
        existing = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ClipError("Could not read the existing clipped note.") from exc
    old_state = _note_semantic_state(existing)
    new_state = _note_semantic_state(content)
    if old_state is None or new_state is None:
        raise ClipError("The existing clipped note has invalid managed metadata.")
    old_meta, _ = old_state
    new_meta, _ = new_state
    if old_meta.get("webclip_id") != new_meta.get("webclip_id"):
        raise ClipError("Refusing to replace a note managed for a different URL.")
    if old_state == new_state:
        return "unchanged"
    if not refresh:
        raise ClipError("The saved page changed; rerun with --refresh to update it.")
    atomic_write(target, _with_manual_section(content, _manual_section(existing)))
    return "written"


def _run_bounded(
    command: Sequence[str],
    *,
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run without a shell while draining pipes into strictly capped buffers."""
    if not isinstance(command, (list, tuple)) or not all(
        isinstance(part, str) for part in command
    ):
        raise ClipError("Internal command construction failed.")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
    except OSError as exc:
        raise ClipError("A required local command could not be started.") from exc

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    exceeded = threading.Event()
    termination_lock = threading.Lock()

    def terminate_group() -> None:
        with termination_lock:
            if process.poll() is not None:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=1)
                return
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    def drain(stream, buffer: bytearray, limit: int) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            remaining = limit - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded.set()
                terminate_group()
                return

    threads = [
        threading.Thread(
            target=drain, args=(process.stdout, stdout_buffer, stdout_limit), daemon=True
        ),
        threading.Thread(
            target=drain, args=(process.stderr, stderr_buffer, stderr_limit), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group()
        returncode = process.returncode if process.returncode is not None else -1
    finally:
        for thread in threads:
            thread.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if timed_out:
        raise ClipError("The local command timed out.")
    if exceeded.is_set():
        raise ClipError("The local command returned too much output.")
    return ProcessResult(returncode, bytes(stdout_buffer), bytes(stderr_buffer))


def _decode_json_object(raw: bytes) -> Mapping[str, object]:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClipError("The extractor returned an invalid response.") from exc
    if not isinstance(payload, dict):
        raise ClipError("The extractor returned an invalid response.")
    return payload


def _extractor_failure(payload: Mapping[str, object]) -> ClipError:
    code = payload.get("code")
    if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
        return ClipError(f"Web extraction failed ({code}).")
    return ClipError("Web extraction failed.")


def _validate_success_payload(data: Mapping[str, object]) -> dict[str, object]:
    limits = {
        "title": 10_000,
        "author": 100_000,
        "published": 10_000,
        "description": 1_000_000,
        "site": 100_000,
        "canonicalUrl": 8192,
        "url": 8192,
        "markdown": 10 * 1024 * 1024,
        "method": 1000,
    }
    checked: dict[str, object] = {}
    for field, limit in limits.items():
        value = data.get(field)
        if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
            raise ClipError("The extractor returned incomplete or invalid article data.")
        checked[field] = value
    if not checked["title"] or not checked["method"]:
        raise ClipError("The extractor returned incomplete or invalid article data.")
    keywords = data.get("keywords")
    if not isinstance(keywords, list) or len(keywords) > 128:
        raise ClipError("The extractor returned incomplete or invalid article data.")
    normalized_keywords: list[str] = []
    for keyword in keywords:
        if not isinstance(keyword, str) or not keyword or len(keyword.encode("utf-8")) > 256:
            raise ClipError("The extractor returned incomplete or invalid article data.")
        normalized_keywords.append(keyword)
    checked["keywords"] = normalized_keywords
    word_count = data.get("wordCount")
    if isinstance(word_count, bool) or not isinstance(word_count, int) or word_count < 0:
        raise ClipError("The extractor returned incomplete or invalid article data.")
    checked["wordCount"] = word_count
    if "ok" in data and data.get("ok") is not True:
        raise _extractor_failure(data)
    source = checked["canonicalUrl"] or checked["url"]
    if not isinstance(source, str):
        raise ClipError("The extractor returned incomplete or invalid article data.")
    normalize_url(source)
    normalize_url(str(checked["url"]))
    return checked


def _extractor_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TZ",
        "PLAYWRIGHT_BROWSERS_PATH",
    )
    child = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    child.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    child.setdefault("HOME", str(Path.home()))
    return child


def run_extractor(
    plugin_root: Path, url: str, no_browser: bool = False
) -> dict[str, object]:
    command = ["node", str(plugin_root / "extractor" / "src" / "cli.mjs"), url]
    if no_browser:
        command.append("--no-browser")
    result = _run_bounded(
        command,
        timeout=EXTRACTOR_TIMEOUT,
        stdout_limit=MAX_STDOUT_BYTES,
        stderr_limit=MAX_STDERR_BYTES,
        cwd=plugin_root / "extractor",
        env=_extractor_environment(),
    )
    try:
        payload = _decode_json_object(result.stdout)
    except ClipError:
        raise
    if result.returncode != 0 or payload.get("ok") is not True:
        raise _extractor_failure(payload)
    return _validate_success_payload(payload)


def _decode_output(result: ProcessResult) -> str:
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClipError("Git returned an invalid local response.") from exc


def _status_paths(raw: bytes) -> set[str]:
    try:
        chunks = raw.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise ClipError("Git returned an invalid status response.") from exc
    paths: set[str] = set()
    index = 0
    while index < len(chunks):
        entry = chunks[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise ClipError("Git returned an invalid status response.")
        status = entry[:2]
        paths.add(entry[3:])
        if "R" in status or "C" in status:
            if index >= len(chunks) or not chunks[index]:
                raise ClipError("Git returned an invalid status response.")
            paths.add(chunks[index])
            index += 1
    return paths


class VaultLock:
    """A non-blocking cross-process lock shared by all Vault writers."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self._handle = None

    def __enter__(self) -> "VaultLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._handle = self.path.open("a+", encoding="utf-8")
            os.chmod(self.path, 0o600)
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(f"pid={os.getpid()}\n")
            self._handle.flush()
        except BlockingIOError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise ClipError("Another Vault write operation is already running.") from exc
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise ClipError("Could not acquire the shared Vault write lock.") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class GitSync:
    """A preflight-approved Git repository that may receive one clip note."""

    def __init__(self, vault: Path, repo_root: Path, branch: str):
        self.vault = vault
        self.repo_root = repo_root
        self.branch = branch

    @staticmethod
    def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> ProcessResult:
        return _run_bounded(
            ["git", "-C", str(repo), *args],
            timeout=timeout,
            stdout_limit=MAX_GIT_OUTPUT_BYTES,
            stderr_limit=MAX_STDERR_BYTES,
        )

    @classmethod
    def preflight(
        cls, vault: Path, expected_branch: str | None = None
    ) -> "GitSync":
        vault = vault.resolve()
        top = cls._git(vault, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise ClipError("Git protection requires the vault to be a Git repository.")
        repo_root = Path(_decode_output(top).strip()).resolve()
        _require_within(vault, repo_root, "The vault Git repository is invalid.")

        branch_result = cls._git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch_result.returncode != 0:
            raise ClipError("Git protection requires an active branch.")
        branch = _decode_output(branch_result).strip()
        if branch.lower() in _PROTECTED_BRANCHES:
            raise ClipError("Refusing to clip on a protected core branch.")
        if expected_branch is not None and branch != expected_branch:
            raise ClipError("The Vault is not on the configured clip sync branch.")

        for state_name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
        ):
            state = cls._git(repo_root, "rev-parse", "--git-path", state_name)
            if state.returncode != 0:
                raise ClipError("Could not verify the Git operation state.")
            state_path = Path(_decode_output(state).strip())
            if not state_path.is_absolute():
                state_path = repo_root / state_path
            if state_path.exists():
                raise ClipError("Refusing to clip during an in-progress Git operation.")

        status = cls._git(
            repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        if status.returncode != 0:
            raise ClipError("Could not verify the Git worktree state.")
        if status.stdout:
            raise ClipError("Git protection requires an entirely clean worktree.")
        if expected_branch is not None:
            remote = cls._git(repo_root, "remote", "get-url", "origin")
            if remote.returncode != 0:
                raise ClipError("Git protection requires an origin remote.")
            fetched = cls._git(repo_root, "fetch", "--prune", "origin")
            if fetched.returncode != 0:
                raise ClipError("Could not fetch the Vault Git remote.")
            upstream = cls._git(
                repo_root,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
            )
            if upstream.returncode != 0:
                raise ClipError("The clip sync branch has no upstream.")
            rebased = cls._git(repo_root, "rebase", _decode_output(upstream).strip())
            if rebased.returncode != 0:
                cls._git(repo_root, "rebase", "--abort")
                raise ClipError("Vault Git rebase conflicted; no clip was written.")
        return cls(vault, repo_root, branch)

    def _relative_paths(self, generated_paths: Sequence[Path]) -> set[str]:
        relative: set[str] = set()
        for path in generated_paths:
            resolved = path.resolve()
            _require_within(resolved, self.vault, "Generated note is outside the vault.")
            _require_within(resolved, self.repo_root, "Generated note is outside Git.")
            relative.add(resolved.relative_to(self.repo_root).as_posix())
        if not relative:
            raise ClipError("No generated note was supplied to Git.")
        return relative

    def finalize(self, generated_paths: Sequence[Path]) -> GitOutcome:
        """Synchronize generated paths, preserving the note on every Git failure."""
        try:
            return self._finalize(generated_paths)
        except ClipError:
            return GitOutcome("verification_failed", "not_attempted")

    def _finalize(self, generated_paths: Sequence[Path]) -> GitOutcome:
        expected = self._relative_paths(generated_paths)
        status = self._git(
            self.repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if status.returncode != 0:
            return GitOutcome("verification_failed", "not_attempted")
        try:
            changed = _status_paths(status.stdout)
        except ClipError:
            return GitOutcome("verification_failed", "not_attempted")
        if not changed.issubset(expected):
            return GitOutcome("refused", "not_attempted")
        if not changed:
            return GitOutcome("unchanged", "not_needed")

        add = self._git(self.repo_root, "add", "--", *sorted(changed))
        if add.returncode != 0:
            return GitOutcome("stage_failed", "not_attempted")

        staged = self._git(self.repo_root, "diff", "--cached", "--name-only", "-z")
        unstaged = self._git(self.repo_root, "diff", "--name-only", "-z")
        if staged.returncode != 0 or unstaged.returncode != 0:
            return GitOutcome("verification_failed", "not_attempted")
        staged_paths = {part for part in _decode_output(staged).split("\0") if part}
        unstaged_paths = {part for part in _decode_output(unstaged).split("\0") if part}
        if staged_paths != changed or unstaged_paths:
            return GitOutcome("verification_failed", "not_attempted")

        commit = self._git(self.repo_root, "commit", "-m", "clip: save web article")
        if commit.returncode != 0:
            return GitOutcome("commit_failed", "not_attempted")
        committed = self._git(
            self.repo_root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-z",
            "-r",
            "HEAD",
        )
        post_status = self._git(
            self.repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if committed.returncode != 0 or post_status.returncode != 0:
            return GitOutcome("committed_unverified", "not_attempted")
        committed_paths = {
            part for part in _decode_output(committed).split("\0") if part
        }
        if committed_paths != expected or post_status.stdout:
            return GitOutcome("committed_unverified", "not_attempted")
        push = self._git(self.repo_root, "push", "-u", "origin", "HEAD")
        if push.returncode != 0:
            return GitOutcome("committed", "push_failed")
        return GitOutcome("committed", "pushed")


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def _ensure_no_secret_markers(note: str) -> None:
    if any(pattern.search(note) for pattern in _SECRET_PATTERNS):
        raise ClipError("The extracted page contains a credential-like marker; refusing to save it.")


class ClipService:
    def __init__(self, plugin_root: Path, env: Mapping[str, str] | None = None):
        self.plugin_root = plugin_root.resolve()
        self.env = env

    def run(self, raw_args: str) -> ClipResult:
        options = parse_clip_args(raw_args)
        config = (
            ClipConfig.from_env(self.env)
            if self.env is not None
            else ClipConfig.from_file(self.plugin_root / "config.toml")
        )
        with VaultLock(config.lock_file):
            return self._run_locked(options, config)

    def _run_locked(self, options: ClipOptions, config: ClipConfig) -> ClipResult:
        git_sync = (
            None
            if options.no_git
            else GitSync.preflight(config.vault, config.sync_branch)
        )

        article = run_extractor(
            self.plugin_root, options.url, no_browser=options.no_browser
        )
        captured_at = datetime.now(timezone.utc)
        note = render_note(article, created=captured_at.isoformat(timespec="seconds"))
        _ensure_no_secret_markers(note)
        source = str(article["canonicalUrl"] or article["url"])

        try:
            config.destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ClipError("Could not create the configured clip destination.") from exc
        destination = config.destination.resolve()
        _require_within(
            destination,
            config.vault,
            "Configured destination escaped the Obsidian vault.",
        )
        target = choose_target(
            destination,
            str(article["title"]),
            source,
            capture_date=captured_at.date().isoformat(),
        )
        write_managed_note(target, note, refresh=options.refresh)

        if git_sync is None:
            outcome = GitOutcome("disabled", "disabled")
        else:
            outcome = git_sync.finalize([target])
        relative_path = target.resolve().relative_to(config.vault).as_posix()
        return ClipResult(relative_path, outcome.commit_state, outcome.push_state)


def build_handler(plugin_root: Path):
    """Create the exception boundary required by Hermes slash commands."""
    service = ClipService(plugin_root)

    def handle(raw_args: str) -> str:
        try:
            return service.run(raw_args).user_message()
        except ClipError as exc:
            return f"Clip failed: {exc}"
        except BaseException:
            # The slash-command boundary must never leak stacks, stderr, or secrets.
            return "Clip failed due to an unexpected local error."

    return handle
