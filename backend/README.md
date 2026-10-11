# BankResilience backend

See the repository README for current setup, architecture, safety model, API, scoring, verification commands and the evaluator demo. Run one loopback-only worker without auto-reload during experiments.

- `main.py`: local Docker adapters, shared experiment lifecycle, SQLite persistence, probes, watchdog and FastAPI endpoints. It owns the bounded outage, latency, packet-loss, CPU-stress and process-pause mutations and their exact inverses.
- `advice.py`: fault-blind Gemini contract/input, evidence references, generic advisory capabilities and observation-supported action checks.
- `control.py`: structured contracts, deterministic safety validation, robust statistics, score and explicit fallback reasoning.
- `reasoning.py`: isolated Gemini JSON interface with bounded request timeouts and validated output; no Docker/shell capabilities.
- `tests/`: guardrails, AI boundary and lifecycle recovery tests.

- `resilience.py`: owned Docker standby/gateway setup, routing journal/rollback, healthy-route validation and bounded Gemini resilience advice contract.
- `gateway.py`: independent primary/standby monitoring and sticky failover; exactly one dispatch per write, authenticated control endpoint and credential-free counters.
- `suite.py`: authenticated local-demo flow checks, bounded optional one-cent payments, measured suite aggregation and final-summary contract.
- Suites use the same bounded experiment lifecycle, stop on unverified recovery and persist in the SQLite `suites` table. Interrupted suites are marked interrupted on restart and never resume injection automatically.
- The default suite is sequential by design. The combinations suite runs latency + CPU and packet loss + CPU, with both components active in each run, bounded cleanup for both mutations, and verified recovery between pairs. POST `/api/suites` with `confirm: true, combinations_only: true, verify_payments: false` to run it. Arbitrary combinations are not supported.

- `planner.py`: dependency-aware weakness ranking, brief evidence justifications, historical combination comparisons, and overall score with explicit single-fault coverage. Served by `/api/analysis`; `/api/planner` retains the validated Gemini experiment-selection contract.
- `findings.py`: exports the three original recovered experiments and saved Gemini recommendations to root `FINDINGS.md`. Run `.venv/bin/python findings.py`; optional `--database` and `--output` select evidence and destination. No injection or payments occur. It also writes `FINDINGS.json` with structured recommendations and a content hash. The dashboard reads `/api/findings`, regenerates it with `POST /api/findings`, and downloads the Markdown from `/api/findings/download`. Generation is blocked during an active experiment, suite, or baseline collection. Separately edited Markdown remains downloadable; refreshing synchronizes its insight cards.
