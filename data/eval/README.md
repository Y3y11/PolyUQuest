# Evaluation datasets

`agent_business_scenarios.sample.*` demonstrates the manifest and behavioral
contract schema. It is a **draft template**, not an approved business Gold Set
and must not be used to claim answer correctness or authorize production
release.

A production dataset should:

1. freeze the authoritative Web evidence snapshot used during annotation;
2. include semantic Gold facts, source patterns, and prohibited claims;
3. cover critical freshness, navigation, multi-hop, abstention, and failure cases;
4. receive a second-person review before its manifest becomes `approved`;
5. update the case-file SHA-256 whenever any case changes;
6. keep baseline reports immutable and review policy changes like code.

Validate a manifest with:

```bash
python -m agent_rag.evaluation.cli validate \
  --manifest data/eval/agent_business_scenarios.sample.manifest.yaml \
  --output data/runtime/eval/dataset-validation.json
```
