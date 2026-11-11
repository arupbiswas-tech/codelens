"""Render the Mermaid diagrams in docs/ARCHITECTURE.md to PNG files (and check their syntax).

GitHub draws Mermaid blocks itself, so this is only needed to produce the PNG copies in
docs/diagrams/ (for slides, the README or places that do not render Mermaid) and to make
sure every diagram still parses after an edit. It uses the same headless browser container as
tools/demo.py; the diagram library is loaded from a CDN, so it needs internet access.

    docker run --rm -v "$PWD:/work" -w /work mcr.microsoft.com/playwright/python:v1.49.0-noble \
        sh -c "pip install -q playwright==1.49.0 && python tools/render_diagrams.py"
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SOURCE = Path("docs/ARCHITECTURE.md")
OUT = Path("docs/diagrams")
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#fff;font-family:system-ui,sans-serif}
#box{display:inline-block;padding:24px;background:#fff}</style></head>
<body><div id="box"></div></body></html>"""


def extract_diagrams(markdown: str) -> list[tuple[str, str]]:
    """Return (file name, mermaid source) for every diagram, named after the section above it."""
    diagrams = []
    heading = "diagram"
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
        if match:
            slug = re.sub(r"[^a-z0-9]+", "-", match.group(2).lower()).strip("-")
            heading = f"{int(match.group(1)):02d}-{slug}"
        if line.strip() == "```mermaid":
            start = index + 1
            while lines[index].strip() != "```" or index == start - 1:
                index += 1
            diagrams.append((heading, "\n".join(lines[start:index])))
        index += 1
    return diagrams


def main() -> int:
    diagrams = extract_diagrams(SOURCE.read_text(encoding="utf-8"))
    if not diagrams:
        print("no mermaid blocks found", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    failed = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=2)
        page.set_content(PAGE)
        page.add_script_tag(url=MERMAID_URL)
        page.evaluate("mermaid.initialize({startOnLoad: false, securityLevel: 'loose', theme: 'default'})")

        for number, (name, source) in enumerate(diagrams, start=1):
            try:
                svg = page.evaluate(
                    "async ([id, src]) => { await mermaid.parse(src); return (await mermaid.render(id, src)).svg; }",
                    [f"d{number}", source],
                )
            except Exception as error:  # noqa: BLE001  (report any Mermaid syntax error and go on)
                print(f"FAILED  diagram {number} ({name}): {str(error).splitlines()[0]}", file=sys.stderr)
                failed += 1
                continue
            page.evaluate("(svg) => { document.getElementById('box').innerHTML = svg; }", svg)
            target = OUT / f"{name}.png"
            page.locator("#box").screenshot(path=str(target))
            print(f"ok      {target}")
        browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
