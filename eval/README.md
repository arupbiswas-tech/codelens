# Evaluation

Measures how good CodeLens retrieval and answers are, on questions I wrote by hand.

```bash
make eval        # retrieval + answers (needs GROQ_API_KEY in .env); 10-25 minutes on the free tier (rate-limit waits)
make eval-tune   # the same, plus the chunk-size / candidate-count experiments
```

Pass extra flags with `ARGS`, for example `make eval ARGS="--no-answers"` for a free, LLM-free, retrieval-only run.
Results go to `eval/results/<time>.json` (everything, per question) and a `latest*.md` file with the tables
(`latest.md` for a full run, `latest-retrieval.md` for `--no-answers`, `latest-no-rerank.md` for `--no-rerank`).

| Flag | What it does |
|---|---|
| `--no-answers` | retrieval only: no LLM calls, free, takes about a minute |
| `--tune` | also run the chunk-size / candidate-count / RRF experiments (retrieval only, also free) |
| `--no-rerank` | answer from the merged search order, to A/B test the reranker (`make eval-no-rerank`) |
| `--resume FILE` | continue an interrupted run: keep its good answers, redo only the failed ones |
| `--pause SECONDS` | wait between questions, to stay under a rate limit |

### LLM quota

A full run makes about 55 LLM calls (29 answers + up to 26 judge calls) and uses roughly 100k tokens.
Groq's free tier allows 200k tokens per day, so about one full run per day fits. When the quota runs out the
run **stops after 3 failed questions in a row**, saves what it has, and the report shows an **INCOMPLETE**
warning. Wait for the quota to reset, then finish the run cheaply:

```bash
make eval ARGS="--resume eval/results/<the-interrupted-run>.json"
```

## The PR reviewer evaluation

```bash
make eval-review                                       # 8 seeded-bug diffs, about 50k LLM tokens
make eval-review ARGS="--resume eval/results/review-<time>.json"   # finish an interrupted run
```

`review_cases.json` holds real `git diff`s made by `make_review_cases.py` (run it again to change the
cases; it clones the pinned commits and applies one mutation each). A **bug** case injects a defect and the
expected line is computed from the diff; a **clean** case is a harmless refactor that should get no comments.
Each case is reviewed once, and three levels are derived from that run: the model's draft, after the rule
check, and final. That shows what each checking layer removed without paying for extra LLM calls.
`latest-review.md` holds the table. As with the other evaluations, repeated failures (usually the daily token
quota) stop the run early and the table says INCOMPLETE.

## What is in `questions.json`

- 29 questions over 4 public repos, each pinned to the commit the labels were written against.
- `expected` lists **alternatives** (finding any one counts). A label is either
  `{"path", "symbol"}` (stable if lines move) or `{"path", "lines": [first, last]}` (for module-level code,
  and for JavaScript, which is chunked as plain line windows).
- `"answerable": false` marks questions the repo cannot answer; the right behaviour is to say so.

## Adding or changing a question

1. Read the code and write a question a developer would really ask. Mix styles: "where is X", "how does Y work",
   exact identifiers, and vague wording.
2. Label the place that answers it. Prefer `symbol`; use `lines` only when there is no function around it.
3. Run `make eval`. The **dataset check** stops the run if a label matches no chunk (a typo, or code that moved),
   and `tests/test_evaluation.py` validates the file's structure.

## How to read the numbers

| Measure | Meaning |
|---|---|
| hit@k | share of questions whose answer chunk is among the top k results |
| MRR | mean of 1/rank of the first correct chunk; rewards ranking it high |
| Faithfulness | share of the answer's claims that the shown code supports (LLM judge) |
| Correct citation | a verified citation points at a labelled chunk |
| Made-up citations | citations naming code the model was never shown (dropped before reaching the user) |

Results are reported as counts (`22/26`), not percentages: with about 25 questions, one question is 4 points,
and a change of one or two questions is noise.

## Limits worth knowing

- Small sample (26 answerable questions): good for catching regressions and obvious problems, not for ranking
  two close settings.
- The judge is the same model that wrote the answers unless `EVAL_JUDGE_MODEL` is set, and models rate their
  own style generously. Use the faithfulness number to compare changes, not as an absolute score.
- Answers come from an LLM and vary slightly between runs even at low temperature.
- Questions were written by the same person who built the system, so they may be biased towards what it handles well.
