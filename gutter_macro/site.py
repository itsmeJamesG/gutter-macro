"""Assemble the publishable static site.

The dashboard needs no server: the page is plain HTML/JS and the "database" is one
JSON file. This step writes that file next to the page so the whole thing can be
served by anything — GitHub Pages, an S3 bucket, `python -m http.server`.

Everything is emitted with relative paths, so the site works both at a domain root
and under a project subpath like /gutter-macro/.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Only these are published. A glob would quietly ship editor backups and anything
# else that happened to be sitting in the directory.
ASSETS = ("index.html", "app.js", "chart.js")


def build_site(payload: dict, out_dir: Path, web_dir: Path | None = None) -> list[Path]:
    """Write `payload` as data.json and copy the page beside it. Returns what it wrote."""
    web = web_dir or WEB_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in ASSETS:
        src = web / name
        if not src.exists():
            raise FileNotFoundError(f"Missing site asset: {src}")
        dst = out_dir / name
        shutil.copyfile(src, dst)
        written.append(dst)

    data = out_dir / "data.json"
    # Compact: this file is downloaded by every visitor, and indentation is ~3x.
    data.write_text(json.dumps(payload, separators=(",", ":")))
    written.append(data)

    # Tell GitHub Pages not to run the output through Jekyll, which would otherwise
    # strip files and directories beginning with an underscore.
    nojekyll = out_dir / ".nojekyll"
    nojekyll.touch()
    written.append(nojekyll)

    return written
