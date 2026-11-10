## Retrieval (26 answerable questions, 4 repos)

| Pipeline | hit@1 | hit@3 | hit@6 | hit@10 | MRR |
|---|---|---|---|---|---|
| vector search only | 12/26 | 16/26 | 24/26 | 26/26 | 0.60 |
| keyword search only | 9/26 | 16/26 | 19/26 | 20/26 | 0.51 |
| hybrid (vector + keyword, RRF) | 13/26 | 19/26 | 22/26 | 23/26 | 0.63 |
| hybrid + reranker | 11/26 | 19/26 | 23/26 | 24/26 | 0.60 |

The merged candidate pool (top 20, before reranking) contains the answer for 24/26 questions: the reranker cannot do better than that.

Questions the final pipeline did not place in the top 3:

- `wpg-01` (where): Where are the user's inputs checked before a plan is generated? -> found at rank 4
- `rec-02` (how): How do I run several statements in one transaction? -> found at rank 8
- `rec-05` (vague): How can I get a single value out of a query result? -> not found in top 10
- `rec-06` (how): Can a column of a row be read as an attribute, like row.name? -> not found in top 10
- `rec-07` (how): How does Records export query results to formats like CSV or JSON? -> found at rank 6
- `itd-01` (how): How does the library check whether a timestamped signature has expired? -> found at rank 5
- `itd-04` (how): How does the URL-safe serializer compress its data? -> found at rank 4
