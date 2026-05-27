"""Cooklang tool unit tests — focused on the security-critical save_recipe path."""

from __future__ import annotations

import pytest

from homelab_mcp.tools.cooklang import NAME_RE


@pytest.mark.parametrize(
    ("name", "expected_ok"),
    [
        # Allowed
        ("easy-pancakes", True),
        ("chicken_tikka", True),
        ("Recipe123", True),
        ("a", True),
        ("X" * 64, True),
        # Path traversal attempts
        ("../etc/passwd", False),
        ("..", False),
        ("foo/bar", False),
        ("foo\\bar", False),
        # Extensions disallowed (we add `.cook` ourselves)
        ("foo.cook", False),
        ("foo.txt", False),
        # Special chars that confuse shells / file systems
        ("foo bar", False),
        ("foo;rm -rf", False),
        ("foo|bar", False),
        ("foo$bar", False),
        ("foo`bar`", False),
        # Null byte injection
        ("foo\x00bar", False),
        # URL / scheme injection
        ("file:///etc/passwd", False),
        ("http://evil.com", False),
        # Empty
        ("", False),
        (" ", False),
        # Unicode tricks (might be valid one day but not today)
        ("recipe\u200b", False),
        ("recipé", False),  # accented char
        ("レシピ", False),  # non-ASCII
    ],
)
def test_name_validation(name: str, expected_ok: bool) -> None:
    """NAME_RE must accept benign filenames and reject every traversal / injection vector."""
    assert bool(NAME_RE.match(name)) is expected_ok
