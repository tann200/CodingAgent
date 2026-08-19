"""
P4-3: Benchmark harness for CodingAgent.

Measures latency and throughput of core pipeline components:
- Planning node: time-to-plan for representative task descriptions
- Execution node: tool dispatch overhead
- Distiller: summary generation latency

Run with:
    pytest tests/benchmarks/ -v
    pytest tests/benchmarks/ -v --benchmark  # show timing table

Requirements: no live LLM (all benchmarks use mocked backends).
"""
