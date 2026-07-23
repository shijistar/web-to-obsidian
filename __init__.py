"""Hermes registration for the standalone web-to-Obsidian plugin."""

from pathlib import Path

from .web_to_obsidian import build_handler


def register(ctx) -> None:
    handler = build_handler(Path(__file__).resolve().parent)
    ctx.register_command(
        "clip",
        handler=handler,
        description="Clip a public web article into an Obsidian vault with guarded Git sync.",
        args_hint="<url> [--refresh] [--no-browser] [--no-git]",
    )
