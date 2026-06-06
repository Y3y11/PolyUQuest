# PolyU RAG Benchmark Dataset

The **PolyU RAG Benchmark** is a 300-question evaluation set over the PolyU
website, designed to test structure-aware, graph-enhanced retrieval. Questions
are stratified into five reasoning types (60 each).

## Layout

```
data/
├── README.md                 # this file
├── DATA_LICENSE.md           # research-use terms (CC BY-NC 4.0 + attribution)
└── dataset_polyu_300.json    # the 300 annotated questions
```

`dataset_polyu_300.json` is a flat JSON array of 300 question objects (60 per
tag). The companion DOM-block corpus that `gold_block_ids` reference is not
shipped here — see [Using `gold_block_ids`](#using-gold_block_ids) below.

## Question schema

Each question is a JSON object. Public (released) fields:

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique id, `q_<tag>_<NN>`. |
| `question` | string | Natural-language question (English). |
| `short_answers` | string[] | Atomic gold answer spans. |
| `reference_answer` | string | Full reference answer prose. |
| `answer_type` | string | e.g. `short`. |
| `tags` | string[] | Reasoning type — see below. |
| `gold_block_ids` | string[] | Hash ids of the gold DOM blocks in the corpus. |
| `gold_mode` | enum | Intended retrieval mode: `mode_a` / `mode_b` / `mode_c` / `hybrid`. |
| `_source_urls` | string[] | Source PolyU page URL(s) the answer comes from. |

> The records also carry internal build-provenance fields (`_build_id`,
> `_source_dataset`, `_source_id`, `_difficulty`) and an `expected_degradation`
> annotation used by the ablation harness. These are not part of the public
> schema — ignore them when consuming the benchmark.

## Question types (60 each, 300 total)

| Tag | Tests | Intended mode |
|---|---|---|
| `single_hop` | one-page factual lookup | `mode_a` |
| `multi_page` | answer spread across linked pages | `mode_b` |
| `entity_list` | enumerate entities / multi-hop over KG | `mode_c` |
| `mixed_ab` | block + page-navigation evidence | `hybrid` |
| `mixed_ac` | block + entity-graph evidence | `hybrid` |

## Using `gold_block_ids`

`gold_block_ids` reference DOM blocks in the indexed corpus. They are only
meaningful against the same corpus build. Two ways to make the release
self-contained are described in the release plan (see project notes): either
ship the block corpus alongside the questions, or pin a fixed corpus snapshot /
build id so the ids stay resolvable.

## License & terms

**Research use only.** The dataset is derived from publicly accessible PolyU web
pages; the per-record `_source_urls` field provides attribution to the
originating pages and must be preserved on redistribution. Full terms are in
[`DATA_LICENSE.md`](DATA_LICENSE.md) (equivalent to CC BY-NC 4.0 with required
`_source_urls` attribution).
