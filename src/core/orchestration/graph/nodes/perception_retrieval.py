import asyncio
import logging
import re
from typing import Any, Mapping

from src.core.orchestration.event_bus import run_with_correlation

logger = logging.getLogger(__name__)


async def _retrieve_context(
    state: Mapping[str, Any],
    orchestrator: Any,
    *,
    symbol_graph_cls: Any,
) -> list:
    """Retrieve repository context snippets for round-0 perception."""
    retrieved_snippets: list = []
    try:
        if (
            state.get("rounds", 0) == 0
            and orchestrator
            and hasattr(orchestrator, "tool_registry")
        ):
            raw_task = state.get("task") or ""
            symbol_regex = re.compile(
                r"`([^`]+)`"
                r'|"([A-Za-z_]\w*)"'
                r"|([A-Z][a-z]+(?:[A-Z][a-z]+)+)"
                r"|([a-z_][a-z0-9]*(?:_[a-z0-9]+){1,})"
            )
            extracted: list = []
            for match in symbol_regex.finditer(raw_task):
                token = next(group for group in match.groups() if group)
                if token and token not in extracted:
                    extracted.append(token)
            query = extracted[0] if extracted else raw_task
            symbol_queries = extracted if extracted else [raw_task]

            loop = asyncio.get_running_loop()

            def _safe_call(tool_name, **kwargs):
                try:
                    tool = orchestrator.tool_registry.get(tool_name)
                    if tool and callable(tool.get("fn")):
                        return tool["fn"](**kwargs)
                except Exception:
                    pass
                return None

            workdir = state.get("working_dir")

            async def _fetch_search_code():
                queries = symbol_queries[:3] if symbol_queries else [query]
                results = await asyncio.gather(
                    *[
                        run_with_correlation(
                            loop,
                            None,
                            lambda _q=_q: _safe_call(
                                "search_code", query=_q, workdir=workdir
                            ),
                        )
                        for _q in queries
                    ],
                    return_exceptions=True,
                )
                merged: list = []
                for result in results:
                    if result and not isinstance(result, Exception):
                        if isinstance(result, dict):
                            merged.extend(result.get("results", []))
                        elif isinstance(result, list):
                            merged.extend(result)
                return {"results": merged} if merged else None

            async def _fetch_symbols():
                results = []
                for symbol_query in symbol_queries[:3]:
                    result = await run_with_correlation(
                        loop,
                        None,
                        lambda sq=symbol_query: _safe_call(
                            "find_symbol", name=sq, workdir=workdir
                        ),
                    )
                    results.append(result)
                return results

            async def _fetch_references():
                return await run_with_correlation(
                    loop,
                    None,
                    lambda: _safe_call("find_references", name=query, workdir=workdir),
                )

            async def _fetch_test_files():
                results = []
                try:
                    if symbol_graph_cls is None:
                        return results
                    symbol_graph = symbol_graph_cls(workdir)
                    for symbol_query in symbol_queries[:2]:
                        tests = await run_with_correlation(
                            loop,
                            None,
                            lambda sq=symbol_query: symbol_graph.find_tests_for_module(sq),
                        )
                        if tests and isinstance(tests, list):
                            results.extend(tests[:2])
                except Exception:
                    pass
                return results

            (
                search_code_result,
                symbol_results,
                references_result,
                test_file_results,
            ) = await asyncio.gather(
                _fetch_search_code(),
                _fetch_symbols(),
                _fetch_references(),
                _fetch_test_files(),
                return_exceptions=True,
            )

            if search_code_result and not isinstance(search_code_result, Exception):
                raw_list = (
                    search_code_result.get("results")
                    if isinstance(search_code_result, dict)
                    else None
                ) or (search_code_result if isinstance(search_code_result, list) else [])
                for result in raw_list:
                    if isinstance(result, dict):
                        retrieved_snippets.append(
                            {
                                "file_path": result.get("file_path") or result.get("file"),
                                "snippet": result.get("snippet")
                                or result.get("text")
                                or result.get("content"),
                                "reason": "search_code",
                            }
                        )

            if (
                symbol_results
                and not isinstance(symbol_results, Exception)
                and isinstance(symbol_results, list)
            ):
                for found_symbol in symbol_results:
                    if (
                        found_symbol
                        and isinstance(found_symbol, dict)
                        and found_symbol.get("file_path")
                    ):
                        retrieved_snippets.append(
                            {
                                "file_path": found_symbol.get("file_path"),
                                "snippet": found_symbol.get("snippet"),
                                "reason": "find_symbol",
                            }
                        )

            if (
                references_result
                and not isinstance(references_result, Exception)
                and isinstance(references_result, list)
            ):
                for reference in references_result:
                    if isinstance(reference, dict):
                        retrieved_snippets.append(
                            {
                                "file_path": reference.get("file_path"),
                                "snippet": reference.get("excerpt")
                                or reference.get("context"),
                                "reason": "find_references",
                            }
                        )

            if (
                test_file_results
                and not isinstance(test_file_results, Exception)
                and isinstance(test_file_results, list)
            ):
                for test_path in test_file_results[:3]:
                    if isinstance(test_path, str) and test_path:
                        retrieved_snippets.append(
                            {
                                "file_path": test_path,
                                "snippet": None,
                                "reason": "find_tests_for_module",
                            }
                        )
    except Exception as retrieval_error:
        logger.debug(
            "perception_node: context retrieval failed (non-fatal, continuing with empty snippets): %s",
            retrieval_error,
        )
        retrieved_snippets = []

    return retrieved_snippets
