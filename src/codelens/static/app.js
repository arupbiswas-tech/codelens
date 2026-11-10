"use strict";

/*
 * CodeLens front end. No framework, no build step.
 *
 * Safety rule: everything that comes from the server (model answers, diff text, file paths)
 * is added to the page as plain text nodes, never parsed as HTML. The helper `h()`
 * below is the only place that creates elements, and it cannot inject markup.
 */

const $ = (id) => document.getElementById(id);

const state = { repos: [], examples: { questions: {}, diffs: [] } };

// ---------------------------------------------------------------- helpers

/** Create an element: h("a", {href, class}, "text", otherNode). Text children stay text. */
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "href") node.setAttribute("href", safeUrl(value));
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

/** Only http(s) links are allowed, so a "javascript:" URL can never be rendered. */
function safeUrl(url) {
  return /^https?:\/\//i.test(url) ? url : "#";
}

class ApiError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

/** Call the API and return its JSON. Failures become an ApiError with a readable message. */
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError("network", "Cannot reach the server. Is it running?");
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    // The body was not JSON; the status code below is enough.
  }
  if (!response.ok) {
    const error = body && body.error;
    throw new ApiError(
      (error && error.code) || `http_${response.status}`,
      (error && error.message) || `The server answered with status ${response.status}.`,
    );
  }
  return body;
}

function postJson(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Run an async action with a disabled button and a "working" message; show errors nicely. */
async function withBusy(button, resultBox, workingText, action) {
  button.disabled = true;
  resultBox.replaceChildren(h("p", { class: "message working" }, workingText));
  try {
    await action();
  } catch (error) {
    const known = error instanceof ApiError;
    resultBox.replaceChildren(
      h("p", { class: "message error", role: "alert" }, known ? error.message : "Something went wrong in the page."),
    );
    if (!known) console.error(error);
  } finally {
    button.disabled = false;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage can be blocked (private windows); the page works without it.
  }
}

function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** A link to the exact lines on GitHub, at the commit that was indexed. */
function blobUrl(repo, path, start, end) {
  if (!repo || !repo.commit_sha) return null;
  const range = end && end !== start ? `L${start}-L${end}` : `L${start}`;
  return `${repo.url}/blob/${repo.commit_sha}/${path}#${range}`;
}

function repoByName(name) {
  return state.repos.find((repo) => repo.name === name) || null;
}

function seconds(startedAt) {
  return ((performance.now() - startedAt) / 1000).toFixed(1);
}

/**
 * Show model text safely: paragraphs, **bold**, `inline code` and ``` fenced code ```.
 * Everything is added as text nodes, so nothing in the answer can become HTML.
 */
function renderText(container, rawText) {
  // Models like to write a non-breaking hyphen, which many fonts draw badly.
  const text = rawText.replaceAll("‑", "-");
  text.split(/```[a-z]*\n?/).forEach((part, index) => {
    if (index % 2 === 1) {
      container.append(h("pre", {}, part.trim()));
      return;
    }
    for (const paragraph of part.split(/\n{2,}/)) {
      if (!paragraph.trim()) continue;
      const p = h("p");
      appendInline(p, paragraph.trim());
      container.append(p);
    }
  });
}

/** Add text to `parent`, turning **bold** and `code` into elements (bold may contain code). */
function appendInline(parent, text) {
  text.split("**").forEach((segment, index) => {
    const holder = index % 2 === 1 ? h("strong") : parent;
    segment.split("`").forEach((piece, i) => holder.append(i % 2 === 1 ? h("code", {}, piece) : piece));
    if (holder !== parent) parent.append(holder);
  });
}

// ---------------------------------------------------------------- tabs

const TABS = ["repos", "ask", "review"];

function selectTab(name, focus = false) {
  for (const tab of TABS) {
    const button = $(`tab-${tab}`);
    const selected = tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`panel-${tab}`).hidden = !selected;
    if (selected && focus) button.focus();
  }
  remember("codelens.tab", name);
}

function setupTabs() {
  TABS.forEach((name, index) => {
    $(`tab-${name}`).addEventListener("click", () => selectTab(name));
    $(`tab-${name}`).addEventListener("keydown", (event) => {
      // Arrow keys move between tabs, as in a standard tab widget.
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (step) selectTab(TABS[(index + step + TABS.length) % TABS.length], true);
    });
  });
  const saved = recall("codelens.tab");
  if (TABS.includes(saved)) selectTab(saved);
}

// ---------------------------------------------------------------- server status

async function checkStatus() {
  const box = $("status");
  try {
    await api("/ready");
    box.textContent = "Server ready";
    box.className = "status ok";
  } catch (error) {
    box.textContent = error instanceof ApiError ? error.message : "Server not ready";
    box.className = "status bad";
  }
}

// ---------------------------------------------------------------- repositories

async function refreshRepos() {
  state.repos = await api("/repos");
  renderRepoList();
  for (const id of ["ask-repo", "review-repo"]) fillRepoSelect($(id));
  showExamples();
}

function fillRepoSelect(select) {
  const previous = select.value || recall(`codelens.${select.id}`);
  select.replaceChildren(
    ...state.repos.map((repo) => h("option", { value: repo.name }, `${repo.name} (${repo.chunks} chunks)`)),
  );
  if (state.repos.length === 0) select.append(h("option", { value: "" }, "No repositories indexed yet"));
  if (previous && repoByName(previous)) select.value = previous;
}

function renderRepoList() {
  const list = $("repo-list");
  if (state.repos.length === 0) {
    list.replaceChildren(h("p", { class: "empty" }, "Nothing is indexed yet. Index a repository above."));
    return;
  }
  list.replaceChildren(
    ...state.repos.map((repo) => {
      const when = new Date(repo.ingested_at).toLocaleString();
      const commit = repo.commit_sha ? repo.commit_sha.slice(0, 8) : "unknown";
      return h(
        "div",
        { class: "repo" },
        h(
          "div",
          {},
          h("div", { class: "name" }, h("a", { href: repo.url }, repo.name)),
          h("div", { class: "detail" }, `${repo.chunks} chunks · commit ${commit} · indexed ${when}`),
        ),
        h("div", {}, askButton(repo), " ", reviewButton(repo)),
      );
    }),
  );
}

function askButton(repo) {
  const button = h("button", { type: "button", class: "chip" }, "Ask");
  button.addEventListener("click", () => {
    $("ask-repo").value = repo.name;
    showExamples();
    selectTab("ask", true);
  });
  return button;
}

function reviewButton(repo) {
  const button = h("button", { type: "button", class: "chip" }, "Review a diff");
  button.addEventListener("click", () => {
    document.querySelector('input[name="review-mode"][value="diff"]').click();
    $("review-repo").value = repo.name;
    showExamples();
    selectTab("review", true);
  });
  return button;
}

function setupIndexForm() {
  $("index-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const startedAt = performance.now();
    await withBusy($("index-button"), $("index-result"), "Indexing... a small repository takes seconds, a large one a minute or two.", async () => {
      const result = await postJson("/repos", { url: $("index-url").value.trim() });
      $("index-result").replaceChildren(
        h(
          "div",
          { class: "message" },
          h("strong", {}, result.repo),
          ` indexed at ${result.commit_sha.slice(0, 8)} in ${seconds(startedAt)} s: `,
          `${result.chunks} chunks (${result.embedded} embedded, ${result.reused} reused, ${result.deleted} removed).`,
        ),
      );
      await refreshRepos();
    });
  });
}

// ---------------------------------------------------------------- ask

function showExamples() {
  const box = $("ask-examples");
  const examples = state.examples.questions[$("ask-repo").value] || [];
  box.replaceChildren(
    ...examples.map((question) => {
      const chip = h("button", { type: "button" }, question);
      chip.addEventListener("click", () => {
        $("ask-question").value = question;
        $("ask-question").focus();
      });
      return chip;
    }),
  );

  const select = $("review-examples");
  const diffs = state.examples.diffs.filter((item) => item.repo === $("review-repo").value);
  select.replaceChildren(
    h("option", { value: "" }, "Choose an example..."),
    ...diffs.map((item) => h("option", { value: item.id }, item.label)),
  );
}

function setupAskForm() {
  $("ask-repo").addEventListener("change", () => {
    remember("codelens.ask-repo", $("ask-repo").value);
    showExamples();
  });
  $("ask-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const startedAt = performance.now();
    await withBusy($("ask-button"), $("ask-result"), "Searching the code and writing an answer...", async () => {
      const repo = repoByName($("ask-repo").value);
      const answer = await postJson("/ask", { repo: $("ask-repo").value, question: $("ask-question").value });
      $("ask-result").replaceChildren(renderAnswer(answer, repo, seconds(startedAt)));
    });
  });
}

function renderAnswer(answer, repo, took) {
  const box = h("div", { class: "answer" });
  renderText(box, answer.answer);

  if (answer.citations.length > 0) {
    box.append(h("p", { class: "meta" }, "Sources (the code the answer is based on):"));
    box.append(
      h(
        "ul",
        { class: "refs" },
        ...answer.citations.map((c) => {
          const label = `${c.path}:L${c.start_line}-L${c.end_line}`;
          const url = blobUrl(repo, c.path, c.start_line, c.end_line);
          return h("li", {}, url ? h("a", { href: url }, label) : label, c.symbol ? ` (${c.symbol})` : "");
        }),
      ),
    );
  }
  const notes = [`answered in ${took} s`];
  if (answer.ungrounded_citations > 0) {
    notes.push(`${answer.ungrounded_citations} reference(s) to code that was not retrieved were removed`);
  }
  box.append(h("p", { class: "meta" }, notes.join(" · ")));
  return box;
}

// ---------------------------------------------------------------- review

function setupReviewForm() {
  document.querySelectorAll('input[name="review-mode"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      const byUrl = reviewMode() === "url";
      $("review-url-field").hidden = !byUrl;
      $("review-diff-fields").hidden = byUrl;
    }),
  );
  $("review-repo").addEventListener("change", () => {
    remember("codelens.review-repo", $("review-repo").value);
    showExamples();
  });
  $("review-examples").addEventListener("change", () => {
    const example = state.examples.diffs.find((item) => item.id === $("review-examples").value);
    if (example) $("review-diff").value = example.diff;
  });

  $("review-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const startedAt = performance.now();
    await withBusy($("review-button"), $("review-result"), "Reviewing... about 2 to 30 seconds per file, depending on the model's load.", async () => {
      const payload = reviewMode() === "url"
        ? { pr_url: $("review-url").value.trim() }
        : { repo: $("review-repo").value, diff: $("review-diff").value };
      const result = await postJson("/review", payload);
      const repoName = payload.repo || repoFromPrUrl(payload.pr_url);
      $("review-result").replaceChildren(renderReview(result, repoByName(repoName), seconds(startedAt)));
    });
  });
}

function reviewMode() {
  return document.querySelector('input[name="review-mode"]:checked').value;
}

function repoFromPrUrl(url) {
  const match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\//.exec(url || "");
  return match ? match[1] : "";
}

function renderReview(result, repo, took) {
  const box = h("div");

  const summary = [`${result.files_reviewed} file(s) reviewed`, `${result.comments.length} comment(s)`];
  const skipped = Object.entries(result.files_skipped).map(([why, n]) => `${n} ${why.replaceAll("_", " ")}`);
  if (skipped.length) summary.push(`skipped: ${skipped.join(", ")}`);
  const dropped = Object.entries(result.dropped).map(([why, n]) => `${n} ${why.replaceAll("_", " ")}`);
  if (dropped.length) summary.push(`rejected by the checks: ${dropped.join(", ")}`);
  summary.push(`${took} s`);
  box.append(h("p", { class: "meta" }, summary.join(" · ")));

  if (result.partial) {
    box.append(h("p", { class: "message warn" }, "The review stopped early (file limit or time limit), so some files were not reviewed."));
  }
  for (const problem of result.errors) {
    box.append(h("p", { class: "message warn" }, problem));
  }

  if (result.comments.length === 0) {
    box.append(h("p", { class: "message" }, "No issues found in the lines this pull request added."));
    return box;
  }

  const byFile = new Map();
  for (const comment of result.comments) {
    if (!byFile.has(comment.path)) byFile.set(comment.path, []);
    byFile.get(comment.path).push(comment);
  }
  for (const [path, comments] of byFile) {
    box.append(
      h("div", { class: "file-group" }, h("h3", {}, path), ...comments.map((c) => renderComment(c, repo))),
    );
  }
  return box;
}

function renderComment(comment, repo) {
  const head = h(
    "div",
    { class: "head" },
    h("span", { class: `badge ${comment.severity}` }, comment.severity),
    h("span", {}, `line ${comment.line}`),
  );
  if (!comment.checked) head.append(h("span", { class: "badge plain" }, "not self-checked"));

  const body = h("div", { class: "comment-body" });
  renderText(body, comment.body);
  const node = h("div", { class: `comment ${comment.severity}` }, head, body);

  if (comment.note) node.append(h("p", { class: "note" }, `Checker: ${comment.note}`));
  if (comment.evidence.length > 0) {
    node.append(
      h(
        "p",
        { class: "meta" },
        "Related code: ",
        ...comment.evidence.flatMap((ref, i) => [i ? ", " : "", evidenceLink(ref, repo)]),
      ),
    );
  }
  return node;
}

/** Turn "src/a.py:L10-L40" into a link to those lines (or plain text if we cannot). */
function evidenceLink(ref, repo) {
  const match = /^(.+):L(\d+)-L(\d+)$/.exec(ref);
  const url = match && blobUrl(repo, match[1], match[2], match[3]);
  return url ? h("a", { href: url }, ref) : ref;
}

// ---------------------------------------------------------------- start

async function loadExamples() {
  try {
    state.examples = await api("/static/examples.json");
  } catch {
    // The page works without examples.
  }
}

async function start() {
  setupTabs();
  setupIndexForm();
  setupAskForm();
  setupReviewForm();
  checkStatus();
  await loadExamples();
  try {
    await refreshRepos();
  } catch (error) {
    $("repo-list").replaceChildren(
      h("p", { class: "message error", role: "alert" }, error instanceof ApiError ? error.message : "Could not load the repositories."),
    );
  }
}

start();
