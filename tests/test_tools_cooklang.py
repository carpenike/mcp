"""Cooklang tool tests.

Two layers:
  * Pure-helper tests for the parsing / sanitisation / authoring logic
    (no I/O) — including the security-critical slug + path-traversal guards.
  * Tool-handler tests driven by an in-memory FakeCook that mirrors the
    real cook.holthome.net wire (metadata `.map` wrapping, server-appended
    `.cook`, silent overwrite, GET-by-path-not-slug).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools import cooklang
from homelab_mcp.tools.cooklang import (
    NAME_RE,
    _abs_to_relpath,
    _assemble_metadata,
    _build_cook_document,
    _flatten_tree,
    _normalize_metadata,
    _parsed_ok,
    _sanitize_relpath,
    _slugify,
    _yaml_frontmatter,
    register,
)

ROOT = "/data/cooklang/recipes"

# Several tool paths return validation errors BEFORE any HTTP call, so the
# catch-all callback legitimately goes unused in those tests.
pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("name", "expected_ok"),
    [
        ("easy-pancakes", True),
        ("chicken_tikka", True),
        ("Recipe123", True),
        ("a", True),
        ("X" * 64, True),
        ("../etc/passwd", False),
        ("..", False),
        ("foo/bar", False),
        ("foo\\bar", False),
        ("foo.cook", False),
        ("foo.txt", False),
        ("foo bar", False),
        ("foo;rm -rf", False),
        ("foo|bar", False),
        ("foo$bar", False),
        ("foo`bar`", False),
        ("foo\x00bar", False),
        ("file:///etc/passwd", False),
        ("http://evil.com", False),
        ("", False),
        (" ", False),
        ("recipe\u200b", False),
        ("recipé", False),
        ("レシピ", False),
    ],
)
def test_name_validation(name: str, expected_ok: bool) -> None:
    """NAME_RE accepts benign slugs and rejects every traversal / injection vector."""
    assert bool(NAME_RE.match(name)) is expected_ok


def test_normalize_metadata_unwraps_map() -> None:
    assert _normalize_metadata({"map": {"id": "x", "title": "X"}}) == {"id": "x", "title": "X"}
    assert _normalize_metadata({"id": "x"}) == {"id": "x"}
    assert _normalize_metadata(None) == {}
    assert _normalize_metadata("nope") == {}


def test_abs_to_relpath_strips_root_and_extension() -> None:
    assert _abs_to_relpath(f"{ROOT}/Smoker/Pork Belly.cook", ROOT) == "Smoker/Pork Belly"
    assert _abs_to_relpath(f"{ROOT}/claude/x.cook", ROOT) == "claude/x"
    # Unknown root prefix is left intact (minus extension).
    assert _abs_to_relpath("/other/x.cook", ROOT) == "/other/x"


def test_flatten_tree() -> None:
    tree = {
        "children": {
            "Smoker": {
                "name": "Smoker",
                "children": {
                    "Pork.cook": {
                        "name": "Pork",
                        "path": f"{ROOT}/Smoker/Pork.cook",
                        "recipe": {
                            "metadata": {
                                "id": "pork",
                                "title": "Pork",
                                "course": "main",
                                "cuisine": "french",
                                "tags": ["smoker", "pork"],
                            }
                        },
                        "children": {},
                    }
                },
            }
        }
    }
    flat = _flatten_tree(tree, ROOT)
    assert len(flat) == 1
    assert flat[0]["slug"] == "pork"
    assert flat[0]["path"] == "Smoker/Pork"
    assert flat[0]["tags"] == ["smoker", "pork"]


def test_slugify() -> None:
    assert _slugify("Calvados-Glazed Pork Belly Bites") == "calvados-glazed-pork-belly-bites"
    assert _slugify("  Mom's Red Stew!  ") == "mom-s-red-stew"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("claude/my-recipe", "claude/my-recipe"),
        ("Smoker/Pork Belly", "Smoker/Pork Belly"),
        ("Confit & Smoked Beef Tongue", "Confit & Smoked Beef Tongue"),
        ("claude/my-recipe.cook", "claude/my-recipe"),
        ("/claude/leading-slash", "claude/leading-slash"),
        ("../../etc/passwd", None),
        ("claude/../../etc", None),
        ("foo/..", None),
        ("foo\\bar", None),
        ("foo\x00bar", None),
        ("", None),
        ("   ", None),
        ("a/b/c", "a/b/c"),
    ],
)
def test_sanitize_relpath(path: str, expected: str | None) -> None:
    assert _sanitize_relpath(path) == expected


def test_yaml_frontmatter_quotes_and_lists() -> None:
    fm = _yaml_frontmatter(
        {
            "title": "Pork: Belly",
            "id": "pork-belly",
            "servings": 8,
            "tags": ["smoker", "pork"],
            "skip": None,
            "empty": [],
        }
    )
    assert 'title: "Pork: Belly"' in fm
    assert 'id: "pork-belly"' in fm
    assert "servings: 8" in fm
    assert "tags:" in fm
    assert '  - "smoker"' in fm
    assert "skip" not in fm
    assert "empty" not in fm


def test_build_cook_document_shape() -> None:
    doc = _build_cook_document({"title": "X", "id": "x"}, "Mix @water{1%cup}.")
    assert doc.startswith("---\n")
    assert "\n---\n\n" in doc
    assert doc.rstrip().endswith("Mix @water{1%cup}.")


def test_assemble_metadata_derived_from_first_class() -> None:
    meta = _assemble_metadata(
        "Fork",
        "fork",
        {"course": "main", "id": "ignored", "title": "ignored"},
        "baseline",
        "calvados swap",
    )
    assert meta["title"] == "Fork"
    assert meta["id"] == "fork"
    assert meta["course"] == "main"
    assert meta["derived_from"] == "baseline"
    assert meta["derived_via"] == "calvados swap"


def test_parsed_ok() -> None:
    assert _parsed_ok(
        {"recipe": {"metadata": {"map": {"id": "x"}}, "ingredients": [{"name": "w"}]}}
    )
    assert not _parsed_ok({"recipe": {"metadata": {"map": {}}, "ingredients": []}})
    assert not _parsed_ok({"nope": True})
    assert not _parsed_ok("bad")


# ─────────────────────────────────────────────────────────────────────────
# FakeCook: in-memory mirror of the cook.holthome.net wire
# ─────────────────────────────────────────────────────────────────────────
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def _strip_scalar(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def _parse_cook(content: str) -> tuple[dict[str, Any], list[str], list[str], str]:
    meta: dict[str, Any] = {}
    body = content
    m = _FRONT_RE.match(content)
    if m:
        fm, body = m.group(1), m.group(2)
        cur_key: str | None = None
        for line in fm.splitlines():
            if line.startswith("  - "):
                if cur_key is not None:
                    bucket = meta.get(cur_key)
                    if not isinstance(bucket, list):
                        bucket = []
                        meta[cur_key] = bucket
                    bucket.append(_strip_scalar(line[4:].strip()))
            elif ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if val == "":
                    cur_key = key
                else:
                    meta[key] = _strip_scalar(val)
                    cur_key = None
    ingredients = [n.strip() for n in re.findall(r"@([^@#~{}\n]+?)\{", body)]
    cookware = [n.strip() for n in re.findall(r"#([^@#~{}\n]+?)\{", body)]
    return meta, ingredients, cookware, body


class FakeCook:
    """Minimal stand-in for the CookLang server, driven via httpx callbacks."""

    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(seed or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        method = request.method
        if path == "/api/recipes" and method == "GET":
            return httpx.Response(200, json=self._tree())
        if path == "/api/search" and method == "GET":
            return httpx.Response(200, json={"results": []})
        if path.startswith("/api/recipes/"):
            relpath = path[len("/api/recipes/") :]
            if relpath.endswith(".cook"):
                relpath = relpath[:-5]
            if method == "GET":
                if request.url.params.get("format") == "raw":
                    content = self.store.get(relpath)
                    if content is None:
                        return httpx.Response(404, json={"error": f"not found: {relpath}"})
                    return httpx.Response(
                        200, text=content, headers={"content-type": "text/plain; charset=utf-8"}
                    )
                return self._get(relpath)
            if method == "PUT":
                self.store[relpath] = request.content.decode("utf-8")
                return httpx.Response(200, json={"path": relpath, "status": "success"})
            if method == "DELETE":
                existed = self.store.pop(relpath, None) is not None
                code = 200 if existed else 404
                return httpx.Response(code, json={"path": relpath, "status": "success"})
        return httpx.Response(404, json={"error": "not found", "path": path})

    def _get(self, relpath: str) -> httpx.Response:
        content = self.store.get(relpath)
        if content is None:
            return httpx.Response(404, json={"error": f"Recipe not found: {relpath}"})
        meta, ing, cw, body = _parse_cook(content)
        return httpx.Response(
            200,
            json={
                "image": None,
                "recipe": {
                    "metadata": {"map": meta},
                    "ingredients": [{"name": n} for n in ing],
                    "cookware": [{"name": n} for n in cw],
                    "steps": [{"value": body}] if body.strip() else [],
                },
            },
        )

    def _tree(self) -> dict[str, Any]:
        root: dict[str, Any] = {"name": "recipes", "children": {}}
        for relpath, content in self.store.items():
            meta, _, _, _ = _parse_cook(content)
            parts = relpath.split("/")
            node = root
            for i, part in enumerate(parts):
                children = node["children"]
                if part not in children:
                    children[part] = {"name": part, "children": {}}
                node = children[part]
                if i == len(parts) - 1:
                    node["path"] = f"{ROOT}/{relpath}.cook"
                    node["recipe"] = {
                        "metadata": meta,
                        "source": {"path": node["path"], "source_type": "Path"},
                    }
        return root


# ─────────────────────────────────────────────────────────────────────────
# Tool-handler fixtures
# ─────────────────────────────────────────────────────────────────────────
class CapturingMCP:
    """Captures the handlers registered via @mcp.tool(name=...)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str) -> Any:
        def deco(fn: Any) -> Any:
            self.tools[name] = fn
            return fn

        return deco


_SEED = {
    "Smoker/Calvados-Glazed Pork Belly Bites": (
        "---\n"
        'title: "Calvados-Glazed Pork Belly Bites"\n'
        'id: "calvados-glazed-pork-belly-bites"\n'
        'course: "appetizer"\n'
        'cuisine: "french"\n'
        'tags:\n  - "smoker"\n  - "pork"\n'
        "---\n\n"
        "Glaze @pork belly{1%kg} with @calvados{50%ml} in a #smoker{}.\n"
    ),
    "Appetizers/Caprese Diem Kabobs": (
        "---\n"
        'title: "Caprese Diem Kabobs"\n'
        'id: "caprese-diem-kabobs"\n'
        'course: "appetizer"\n'
        'cuisine: "italian"\n'
        'tags:\n  - "griddle"\n'
        "---\n\n"
        "Skewer @mozzarella{200%g} and @basil{1%leaf}.\n"
    ),
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        oauth_required=False,
        cooklang_base_url="https://cook.test",
        federation_base_url="https://fed.test",
        recipes_dir=ROOT,
    )


@pytest.fixture
def tools(settings: Settings) -> dict[str, Any]:
    mcp = CapturingMCP()
    register(mcp, settings)  # type: ignore[arg-type]
    return mcp.tools


@pytest.fixture
def fake(httpx_mock: Any) -> FakeCook:
    server = FakeCook(seed=dict(_SEED))
    httpx_mock.add_callback(server, is_reusable=True)
    return server


# ─────────────────────────────────────────────────────────────────────────
# Tool handlers
# ─────────────────────────────────────────────────────────────────────────
async def test_list_recipes(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_list_recipes"]()
    assert res["returned"] == 2
    assert res["total"] == 2
    assert res["truncated"] is False
    slugs = {r["slug"] for r in res["recipes"]}
    assert "calvados-glazed-pork-belly-bites" in slugs
    paths = {r["path"] for r in res["recipes"]}
    assert "Smoker/Calvados-Glazed Pork Belly Bites" in paths


async def test_list_recipes_filters(tools: dict[str, Any], fake: FakeCook) -> None:
    by_cuisine = await tools["cooklang_list_recipes"](cuisine="french")
    assert by_cuisine["returned"] == 1
    by_tag = await tools["cooklang_list_recipes"](tag="griddle")
    assert by_tag["returned"] == 1
    by_query = await tools["cooklang_list_recipes"](query="caprese")
    assert by_query["returned"] == 1


async def test_list_recipes_truncation_contract(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_list_recipes"](limit=1)
    assert res["returned"] == 1
    assert res["total"] == 2
    assert res["truncated"] is True


async def test_list_recipes_ranked_query(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_list_recipes"](query="pork smoker")
    assert res["returned"] >= 1
    assert res["recipes"][0]["slug"] == "calvados-glazed-pork-belly-bites"


async def test_list_recipes_match_ingredients(tools: dict[str, Any], fake: FakeCook) -> None:
    # Ingredient-only match: not found in metadata, found with the opt-in scan.
    shallow = await tools["cooklang_list_recipes"](query="mozzarella")
    assert shallow["returned"] == 0
    deep = await tools["cooklang_list_recipes"](query="mozzarella", match_ingredients=True)
    assert deep["returned"] == 1
    assert deep["recipes"][0]["slug"] == "caprese-diem-kabobs"


async def test_temp_artifacts_filtered_from_listings(tools: dict[str, Any], fake: FakeCook) -> None:
    # An orphaned validation artifact must never surface in a listing.
    fake.store["claude/zz-tmp-orphan"] = _SEED["Appetizers/Caprese Diem Kabobs"]
    res = await tools["cooklang_list_recipes"]()
    assert res["returned"] == 2
    assert not any("zz-tmp-" in r["path"] for r in res["recipes"])


async def test_non_json_upstream_returns_structured_error(
    tools: dict[str, Any], httpx_mock: Any
) -> None:
    # A proxy/SSO 200 HTML page must map to a structured error, never a raise.
    httpx_mock.add_response(status_code=200, text="<html>login</html>", is_reusable=True)
    res = await tools["cooklang_list_recipes"]()
    assert "error" in res
    assert res["error"]["code"].startswith("cooklang_http")


async def test_get_recipe_by_path(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_get_recipe"](identifier="Smoker/Calvados-Glazed Pork Belly Bites")
    assert res["slug"] == "calvados-glazed-pork-belly-bites"
    assert res["metadata"]["cuisine"] == "french"
    assert any(i["name"] == "pork belly" for i in res["ingredients"])
    assert any(c["name"] == "smoker" for c in res["cookware"])


async def test_get_recipe_exposes_source(tools: dict[str, Any], fake: FakeCook) -> None:
    # get_recipe must return the raw `.cook` source so the model can edit it
    # without reconstructing (and losing formatting).
    res = await tools["cooklang_get_recipe"](identifier="calvados-glazed-pork-belly-bites")
    assert res["source"] is not None
    assert "@pork belly{1%kg}" in res["source"]
    assert res["source"].startswith("---\n")


async def test_get_recipe_by_slug(tools: dict[str, Any], fake: FakeCook) -> None:
    # Slug does not resolve a direct GET; resolver must fall back to the tree.
    res = await tools["cooklang_get_recipe"](identifier="calvados-glazed-pork-belly-bites")
    assert res["path"] == "Smoker/Calvados-Glazed Pork Belly Bites"


async def test_get_recipe_not_found(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_get_recipe"](identifier="does-not-exist")
    assert res["error"]["code"] == "recipe_not_found"


async def test_search_recipes_tool_removed(tools: dict[str, Any]) -> None:
    # cooklang_search_recipes was merged into cooklang_list_recipes.
    assert "cooklang_search_recipes" not in tools
    assert "cooklang_list_recipes" in tools


async def test_create_recipe_happy_path(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_create_recipe"](
        title="Calvados-Glazed Pork Belly Bites Fork",
        slug="calvados-glazed-pork-belly-bites-fork",
        body="Glaze @pork belly{1%kg} with @calvados{50%ml} in a #smoker{}.",
        derived_from="calvados-glazed-pork-belly-bites",
        derived_via="extra glaze",
        metadata={"course": "appetizer", "tags": ["smoker", "fork"]},
    )
    assert res["ok"] is True
    assert res["slug"] == "calvados-glazed-pork-belly-bites-fork"
    assert res["path"] == "claude/calvados-glazed-pork-belly-bites-fork"
    stored = fake.store["claude/calvados-glazed-pork-belly-bites-fork"]
    assert "derived_from" in stored
    assert "calvados-glazed-pork-belly-bites" in stored
    # No temp files left behind.
    assert not any(k.startswith("claude/zz-tmp-") for k in fake.store)


async def test_create_recipe_rejects_collision(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/dup"] = _SEED["Appetizers/Caprese Diem Kabobs"]
    res = await tools["cooklang_create_recipe"](title="Dup", slug="dup", body="Mix @water{1%cup}.")
    assert res["error"]["code"] == "recipe_exists"


async def test_create_recipe_overwrite(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/dup"] = _SEED["Appetizers/Caprese Diem Kabobs"]
    res = await tools["cooklang_create_recipe"](
        title="Dup", slug="dup", body="Mix @water{1%cup}.", overwrite=True
    )
    assert res["ok"] is True


async def test_create_recipe_invalid_slug(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_create_recipe"](
        title="Bad", slug="../escape", body="Mix @water{1%cup}."
    )
    assert res["error"]["code"] == "invalid_slug"
    assert "../escape" not in str(list(fake.store.keys()))


async def test_create_recipe_rejects_metadata_key_injection(
    tools: dict[str, Any], fake: FakeCook
) -> None:
    # A metadata KEY carrying a newline + a second frontmatter line must be
    # rejected before any write (YAML frontmatter injection).
    res = await tools["cooklang_create_recipe"](
        title="Bad",
        slug="badmeta",
        body="Mix @water{1%cup}.",
        metadata={"foo: x\nsource": "evil"},
    )
    assert res["error"]["code"] == "invalid_metadata_key"
    assert "claude/badmeta" not in fake.store


async def test_create_recipe_rejects_metadata_value_newline(
    tools: dict[str, Any], fake: FakeCook
) -> None:
    # A VALUE with a literal newline (here via the title) must be rejected.
    res = await tools["cooklang_create_recipe"](
        title="Bad\nid: evil", slug="badval", body="Mix @water{1%cup}."
    )
    assert res["error"]["code"] == "invalid_metadata_value"
    assert "claude/badval" not in fake.store


async def test_create_recipe_rejects_traversal_folder(
    tools: dict[str, Any], fake: FakeCook
) -> None:
    res = await tools["cooklang_create_recipe"](
        title="Bad", slug="ok", folder="../../etc", body="Mix @water{1%cup}."
    )
    assert res["error"]["code"] == "invalid_folder"


async def test_create_recipe_empty_body(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_create_recipe"](title="Empty", slug="empty", body="   ")
    assert res["error"]["code"] == "empty_body"


async def test_update_recipe_not_found(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_update_recipe"](slug="ghost", body="Mix @water{1%cup}.")
    assert res["error"]["code"] == "recipe_not_found"


async def test_update_recipe_merges_metadata(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/edit-me"] = (
        '---\ntitle: "Edit Me"\nid: "edit-me"\ncourse: "main"\n---\n\nBoil @water{1%L}.\n'
    )
    res = await tools["cooklang_update_recipe"](
        slug="edit-me",
        body="Boil @water{2%L} hard.",
        derived_from="some-baseline",
    )
    assert res["ok"] is True
    stored = fake.store["claude/edit-me"]
    assert "derived_from" in stored
    assert 'course: "main"' in stored  # preserved
    assert "2%L" in stored  # body replaced


async def test_update_recipe_metadata_only_reuses_source(
    tools: dict[str, Any], fake: FakeCook
) -> None:
    # With body omitted, the current raw source is reused so the body (incl.
    # its formatting) is preserved while only metadata changes.
    fake.store["claude/keep-body"] = (
        "---\n"
        'title: "Keep Body"\n'
        'id: "keep-body"\n'
        "---\n\n"
        "Simmer @sauce{1%cup} slowly and precisely.\n"
    )
    res = await tools["cooklang_update_recipe"](slug="keep-body", metadata={"course": "main"})
    assert res["ok"] is True
    stored = fake.store["claude/keep-body"]
    assert "Simmer @sauce{1%cup} slowly and precisely." in stored  # body preserved
    assert 'course: "main"' in stored  # metadata added


async def test_update_recipe_moves_to_new_folder(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/relocate-me"] = (
        '---\ntitle: "Relocate Me"\nid: "relocate-me"\n---\n\nBoil @water{1%L}.\n'
    )
    res = await tools["cooklang_update_recipe"](
        slug="relocate-me", body="Boil @water{1%L}.", new_folder="Smoker"
    )
    assert res["ok"] is True
    assert res["path"] == "Smoker/relocate-me"
    assert res["moved_from"] == "claude/relocate-me"
    # Old gone, new present.
    assert "claude/relocate-me" not in fake.store
    assert "Smoker/relocate-me" in fake.store


async def test_update_recipe_renames_slug(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/old-name"] = (
        '---\ntitle: "Old Name"\nid: "old-name"\n---\n\nBoil @water{1%L}.\n'
    )
    res = await tools["cooklang_update_recipe"](
        slug="old-name", body="Boil @water{1%L}.", new_slug="new-name"
    )
    assert res["ok"] is True
    assert res["path"] == "claude/new-name"
    assert res["slug"] == "new-name"
    assert "claude/old-name" not in fake.store
    assert 'id: "new-name"' in fake.store["claude/new-name"]


async def test_update_recipe_move_collision_guarded(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/mover"] = '---\ntitle: "Mover"\nid: "mover"\n---\n\nBoil @water{1%L}.\n'
    fake.store["Smoker/mover"] = '---\ntitle: "Existing"\nid: "mover"\n---\n\nGrill @beef{1%lb}.\n'
    blocked = await tools["cooklang_update_recipe"](
        slug="claude/mover", body="Boil @water{1%L}.", new_folder="Smoker"
    )
    assert blocked["error"]["code"] == "destination_exists"
    assert "claude/mover" in fake.store  # original untouched
    # With overwrite it succeeds and removes the original.
    ok = await tools["cooklang_update_recipe"](
        slug="claude/mover", body="Boil @water{1%L}.", new_folder="Smoker", overwrite=True
    )
    assert ok["ok"] is True
    assert "claude/mover" not in fake.store


async def test_update_recipe_rejects_bad_move_targets(
    tools: dict[str, Any], fake: FakeCook
) -> None:
    fake.store["claude/safe"] = '---\ntitle: "Safe"\nid: "safe"\n---\n\nBoil @water{1%L}.\n'
    bad_folder = await tools["cooklang_update_recipe"](
        slug="safe", body="Boil @water{1%L}.", new_folder="../../etc"
    )
    assert bad_folder["error"]["code"] == "invalid_folder"
    bad_slug = await tools["cooklang_update_recipe"](
        slug="safe", body="Boil @water{1%L}.", new_slug="../escape"
    )
    assert bad_slug["error"]["code"] == "invalid_slug"
    # Nothing escaped the recipe tree.
    assert not any(".." in k for k in fake.store)


async def test_delete_recipe_requires_confirmation(tools: dict[str, Any], fake: FakeCook) -> None:
    # Without confirm=true the tool previews but must NOT delete.
    res = await tools["cooklang_delete_recipe"](slug="calvados-glazed-pork-belly-bites")
    assert res["ok"] is False
    assert res["requires_confirmation"] is True
    assert res["path"] == "Smoker/Calvados-Glazed Pork Belly Bites"
    assert res["title"] == "Calvados-Glazed Pork Belly Bites"
    # Still present.
    assert "Smoker/Calvados-Glazed Pork Belly Bites" in fake.store


async def test_delete_recipe_confirmed(tools: dict[str, Any], fake: FakeCook) -> None:
    fake.store["claude/kill-me"] = (
        '---\ntitle: "Kill Me"\nid: "kill-me"\n---\n\nBoil @water{1%L}.\n'
    )
    res = await tools["cooklang_delete_recipe"](slug="kill-me", confirm=True)
    assert res["ok"] is True
    assert res["deleted"] == "claude/kill-me"
    assert res["slug"] == "kill-me"
    assert "claude/kill-me" not in fake.store


async def test_delete_recipe_not_found(tools: dict[str, Any], fake: FakeCook) -> None:
    res = await tools["cooklang_delete_recipe"](slug="ghost", confirm=True)
    assert res["error"]["code"] == "recipe_not_found"


async def test_create_recipe_validation_failure_no_write(
    tools: dict[str, Any], fake: FakeCook, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the parser round-trip to report invalid; the real target must
    # NOT be written and no temp files may remain.
    monkeypatch.setattr(cooklang, "_parsed_ok", lambda _data: False)
    res = await tools["cooklang_create_recipe"](
        title="Will Fail", slug="will-fail", body="Mix @water{1%cup}."
    )
    assert res["error"]["code"] == "validation_failed"
    assert "claude/will-fail" not in fake.store
    assert not any(k.startswith("claude/zz-tmp-") for k in fake.store)


async def test_list_recipes_blank_query_lists_all(tools: dict[str, Any], fake: FakeCook) -> None:
    # A blank query is no longer an error — it just lists everything.
    res = await tools["cooklang_list_recipes"](query="   ")
    assert res["returned"] == 2
