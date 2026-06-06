# Baseline recordings

The `/compare` page renders 3 panels (LightRAG | HTMLRAG | Ours). Ours streams from
`/api/query/stream`; LightRAG and HTMLRAG **replay** pre-recorded JSON to keep the
comparison reproducible and decoupled from each baseline's runtime.

## Slug rule

Implemented in `frontend/lib/baselineLoader.ts` (`slugifyQuestion`):

1. Lowercase.
2. Replace every non-alphanumeric, non-whitespace character with a space.
3. Split on whitespace, drop empty tokens.
4. Take the first **6** tokens.
5. Join with `_`.

This is a one-way slug. If a question is reworded, regenerate the JSON.

## Current preset → slug mapping

| Mode | Question | Slug |
|---|---|---|
| A | `What is Prof. Li's research direction?` | `what_is_prof_li_s_research` |
| B | `What are the admission requirements for MSc DSA?` | `what_are_the_admission_requirements_for` |
| C | `Which professors in COMP do NLP research?` | `which_professors_in_comp_do_nlp` |

## File layout

```
public/baselines/
  lightrag/<slug>.json
  htmlrag/<slug>.json
```

A missing file is **not** an error — the UI shows a "No recording for X" panel with
the expected path. This is intentional so we can ship the demo before every baseline
recording lands.

## Schema (`BaselineRecord` in `lib/baselineLoader.ts`)

```jsonc
{
  "system_name": "LightRAG",        // displayed in pane header
  "mode_label": "Entity-centric",   // subtitle (one line, descriptive)
  "answer": "Markdown with [1][2] citations…",
  "blocks": [                       // BlockRef[], same shape as our SSE
    {
      "block_id": "...",
      "content": "...",
      "heading_context": "...",
      "source_url": "...",
      "source_title": "...",
      "score": 0.78
    }
  ],
  "pipeline_trace": [               // PipelineStep[]; REQUIRED for visual parity
    {
      "step": "vector_search",
      "label": "Vector search",
      "duration_ms": 142,
      "data": {}
    }
  ],
  "elapsed_seconds": 4.2,           // total wall-clock; drives the replay pace
  "recorded_at": "2026-05-20",      // optional
  "notes": "Run on commit abc1234"  // optional
}
```

### Why `pipeline_trace` is required

The mini-Theatre under each baseline pane shows a Pipeline tab. Without trace data
that tab is empty — and judges will notice the asymmetry against the Ours pane.
At minimum, populate `pipeline_trace` with the baseline's actual top-level stages
(e.g. for LightRAG: `["query_decompose", "entity_extract", "kg_retrieve", "answer_generate"]`).

### Why `blocks` is required

The Blocks tab + the cross-system overlap metric (Jaccard on `block_id`) both
need real `block_id`s. If the baseline doesn't return our block schema directly,
write a small recorder that maps its sources back to our `block_id`s — otherwise
overlap will read 0% and the metric is meaningless.

## Workflow

1. Run the baseline on each preset question.
2. Write the JSON to the corresponding slug path.
3. Refresh `/compare` — no rebuild needed (files are served from `public/`).

Placeholder files committed at each slug path describe the expected fields and
emit a soft "[placeholder]" tag in the answer so the demo never silently shows
fake numbers.
