"""Environment-isolated entrypoint for the admission control-plane benchmark."""

from __future__ import annotations

import os


def main() -> None:
    # The benchmark never loads embeddings or calls a provider. Override only in
    # this CLI process so a developer's local-ML .env cannot make the control-plane
    # benchmark depend on optional model packages.
    os.environ["APP_RUNTIME_PROFILE"] = "auto"
    os.environ["EMBEDDING_PROVIDER"] = "siliconflow"
    from agent_rag.runs.benchmark import main as benchmark_main

    benchmark_main()


if __name__ == "__main__":
    main()
