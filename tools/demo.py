"""Drive the CodeLens web UI in a headless browser: check it works and capture the demo.

Developer tool, not part of the app. It runs in a throwaway Playwright container, on the same
Docker network as the app. From the project root:

    make demo            # everything, including the LLM steps (needs GROQ_API_KEY and quota)
    make demo ARGS=--no-llm   # only the steps that need no LLM

It exits non-zero if anything the user should see is missing, so it doubles as an end-to-end
test of the UI. Screenshots go to docs/screens/, and --gif also builds docs/demo.gif.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

OUT = Path("docs/screens")
LLM_WAIT_MS = 120_000  # an answer can wait on the provider's rate limit

RECORDS = "kennethreitz/records"


class Recorder:
    """Takes numbered screenshots and remembers their captions for the GIF."""

    def __init__(self, page: Page):
        self.page = page
        self.frames: list[tuple[Path, str]] = []

    def shot(self, name: str, caption: str) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{len(self.frames) + 1:02d}-{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        self.frames.append((path, caption))
        print(f"  screenshot {path} - {caption}")


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  ok: {message}")


def step_repositories(page: Page, rec: Recorder) -> None:
    print("Repositories tab")
    expect(page.locator("#status")).to_have_text("Server ready", timeout=30_000)
    expect(page.locator(".repo").first).to_be_visible(timeout=30_000)
    names = page.locator(".repo .name").all_inner_texts()
    check(RECORDS in names, f"the repository list shows {RECORDS} (found {len(names)} repos)")
    rec.shot("repositories", "Indexed repositories")


def step_index_error(page: Page, rec: Recorder) -> None:
    print("Indexing a bad URL shows a clear error")
    page.fill("#index-url", "https://example.com/not/github")
    page.click("#index-button")
    expect(page.locator("#index-result .message.error")).to_contain_text("github.com/owner/repo", timeout=15_000)
    check(True, "a non-GitHub URL gives a readable error, not a crash")
    rec.shot("index-error", "A clear error for a bad URL")


def step_ask(page: Page, rec: Recorder) -> None:
    print("Ask: an answerable question")
    page.click("#tab-ask")
    page.select_option("#ask-repo", RECORDS)
    page.locator("#ask-examples button").first.click()
    check(len(page.input_value("#ask-question")) > 10, "clicking an example fills the question box")
    page.click("#ask-button")
    expect(page.locator("#ask-result .answer")).to_be_visible(timeout=LLM_WAIT_MS)
    links = page.locator("#ask-result .refs a")
    check(links.count() >= 1, f"the answer lists {links.count()} source link(s)")
    href = links.first.get_attribute("href")
    check(href.startswith(f"https://github.com/{RECORDS}/blob/"), f"a source links to GitHub lines: {href}")
    rec.shot("ask-answer", "An answer with links to the exact code")

    print("Ask: a question the repo cannot answer")
    page.locator("#ask-examples button").nth(3).click()  # the asyncio question
    page.click("#ask-button")
    expect(page.locator("#ask-result .answer")).to_contain_text("could not find", timeout=LLM_WAIT_MS)
    check(True, "an unanswerable question is refused instead of guessed")
    rec.shot("ask-refusal", "It says so when the code has no answer")


def step_review(page: Page, rec: Recorder) -> None:
    print("Review: a diff with an injected bug")
    page.click("#tab-review")
    page.check('input[name="review-mode"][value="diff"]')
    page.select_option("#review-repo", RECORDS)
    page.select_option("#review-examples", "rec-scalar")
    check("row[1]" in page.input_value("#review-diff"), "choosing an example fills the diff")
    rec.shot("review-form", "Paste a diff, or pick an example")
    page.click("#review-button")
    expect(page.locator("#review-result .comment").first).to_be_visible(timeout=LLM_WAIT_MS)
    comment = page.locator("#review-result .comment").first
    check("line 255" in comment.inner_text(), "the comment is on line 255, the injected bug")
    check(comment.locator(".badge.high").count() == 1, "it is marked high severity")
    rec.shot("review-comment", "The injected bug is found on the right line")

    print("Review: a harmless change")
    page.select_option("#review-repo", "pallets/itsdangerous")  # the example list follows the repo
    page.select_option("#review-examples", "itd-clean")
    page.click("#review-button")
    expect(page.locator("#review-result")).to_contain_text("file(s) reviewed", timeout=LLM_WAIT_MS)
    check(page.locator("#review-result .comment").count() == 0, "a harmless change gets no comments")
    rec.shot("review-clean", "A harmless change gets no comments")


def build_gif(frames: list[tuple[Path, str]], target: Path) -> None:
    """Join the screenshots into a looping GIF with a caption strip."""
    from PIL import Image, ImageDraw

    images = []
    for path, caption in frames:
        shot = Image.open(path).convert("RGB")
        width = 900
        shot = shot.resize((width, int(shot.height * width / shot.width)))
        shot = shot.crop((0, 0, width, min(shot.height, 620)))  # the top of the page is what matters
        canvas = Image.new("RGB", (width, shot.height + 36), (23, 29, 39))
        canvas.paste(shot, (0, 36))
        ImageDraw.Draw(canvas).text((14, 11), caption, fill=(230, 234, 242))
        images.append(canvas.quantize(colors=96, method=Image.Quantize.MEDIANCUT))
    target.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(target, save_all=True, append_images=images[1:], duration=2800, loop=0, optimize=True)
    print(f"wrote {target} ({target.stat().st_size // 1024} KB, {len(images)} frames)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the CodeLens UI in a browser.")
    parser.add_argument("--base", default="http://app:8000", help="URL of the running app")
    parser.add_argument("--no-llm", action="store_true", help="skip the steps that call the LLM")
    parser.add_argument("--gif", action="store_true", help="also build docs/demo.gif")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda error: (print(f"FAILED: page error: {error}", file=sys.stderr), sys.exit(1)))
        page.goto(args.base)
        rec = Recorder(page)

        step_repositories(page, rec)
        step_index_error(page, rec)
        if not args.no_llm:
            step_ask(page, rec)
            step_review(page, rec)
        browser.close()

    if args.gif:
        build_gif(rec.frames, Path("docs/demo.gif"))
    print("UI check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
