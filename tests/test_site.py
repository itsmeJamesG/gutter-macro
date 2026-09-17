"""Tests for static site assembly.

The published dashboard has no server, so anything wrong here ships as a broken
page rather than a 500 someone notices. These pin the properties that make the
output servable from anywhere: relative references only, no stray files, and a
data.json the page can actually parse.
"""
from __future__ import annotations

import json

import pytest

from gutter_macro.site import ASSETS, build_site


@pytest.fixture
def web(tmp_path):
    """A stand-in for web/ — the real assets are exercised by the path tests below."""
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text('<script type="module" src="./app.js"></script>')
    (d / "app.js").write_text('import "./chart.js"; fetch("./data.json");')
    (d / "chart.js").write_text("export const x = 1;")
    return d


def test_writes_page_and_data_together(tmp_path, web):
    out = tmp_path / "site"
    build_site({"panels": []}, out, web_dir=web)

    for name in ASSETS:
        assert (out / name).exists(), name
    assert (out / "data.json").exists()


def test_data_json_round_trips(tmp_path, web):
    payload = {"ranges": [1, 3, 5, 10], "panels": [], "latest_date": "2026-09-15"}
    out = tmp_path / "site"
    build_site(payload, out, web_dir=web)

    assert json.loads((out / "data.json").read_text()) == payload


def test_data_json_is_compact(tmp_path, web):
    """Every visitor downloads this file; indentation roughly triples it."""
    payload = {"panels": [{"key": "gas", "series": [{"points": [["2026-01-01", 1.0]] * 50}]}]}
    out = tmp_path / "site"
    build_site(payload, out, web_dir=web)
    text = (out / "data.json").read_text()

    assert "\n" not in text
    assert ", " not in text


def test_nojekyll_is_written(tmp_path, web):
    """Without it, Pages runs the output through Jekyll and drops underscore files."""
    out = tmp_path / "site"
    build_site({}, out, web_dir=web)

    assert (out / ".nojekyll").exists()


def test_only_declared_assets_are_published(tmp_path, web):
    """A glob would ship editor backups and scratch files sitting in web/."""
    (web / "notes.md.bak").write_text("scratch")
    (web / "TODO.txt").write_text("secret plans")
    out = tmp_path / "site"
    build_site({}, out, web_dir=web)

    published = {p.name for p in out.iterdir()}
    assert published == {*ASSETS, "data.json", ".nojekyll"}


def test_missing_asset_fails_loudly(tmp_path, web):
    """Silently publishing a site with no app.js yields a blank page and no error."""
    (web / "app.js").unlink()
    with pytest.raises(FileNotFoundError, match="app.js"):
        build_site({}, tmp_path / "site", web_dir=web)


def test_rebuild_overwrites_cleanly(tmp_path, web):
    out = tmp_path / "site"
    build_site({"v": 1}, out, web_dir=web)
    build_site({"v": 2}, out, web_dir=web)

    assert json.loads((out / "data.json").read_text()) == {"v": 2}


def test_creates_nested_output_directory(tmp_path, web):
    out = tmp_path / "a" / "b" / "site"
    build_site({}, out, web_dir=web)

    assert (out / "index.html").exists()


def test_returns_everything_it_wrote(tmp_path, web):
    written = build_site({}, tmp_path / "site", web_dir=web)

    assert {p.name for p in written} == {*ASSETS, "data.json", ".nojekyll"}


# ------------------------------------------------- the real assets, not fixtures


def test_real_page_uses_only_relative_references():
    """GitHub Pages serves a project site from /<repo>/, so any absolute src, href
    or fetch target 404s there while working fine at a domain root — the failure
    only appears once deployed."""
    from gutter_macro.site import WEB_DIR

    for name in ASSETS:
        text = (WEB_DIR / name).read_text()
        for bad in ('src="/', "src='/", 'href="/static', 'fetch("/', "fetch('/"):
            assert bad not in text, f"{name} contains an absolute reference: {bad}"


def test_real_page_fetches_data_relatively():
    from gutter_macro.site import WEB_DIR

    assert 'fetch("./data.json")' in (WEB_DIR / "app.js").read_text()


def test_real_assets_all_exist():
    from gutter_macro.site import WEB_DIR

    for name in ASSETS:
        assert (WEB_DIR / name).exists(), f"web/{name} is missing"
