import asyncio

from src.core.inference.provider_probe import validate_provider_connection


class _SyncValidAdapter:
    def validate_connection(self):
        return True


class _AsyncValidAdapter:
    async def validate_connection(self):
        return True


class _ProbeOnlyAdapter:
    def get_models_from_api(self):
        return {"models": [{"id": "x"}]}


class _BrokenProbeAdapter:
    def get_models_from_api(self):
        raise RuntimeError("boom")


def test_validate_provider_connection_uses_sync_validator():
    assert asyncio.run(validate_provider_connection(adapter=_SyncValidAdapter())) is True


def test_validate_provider_connection_awaits_async_validator():
    assert asyncio.run(validate_provider_connection(adapter=_AsyncValidAdapter())) is True


def test_validate_provider_connection_falls_back_to_model_probe():
    assert asyncio.run(validate_provider_connection(adapter=_ProbeOnlyAdapter())) is True


def test_validate_provider_connection_returns_false_on_probe_error_or_missing_adapter():
    assert asyncio.run(validate_provider_connection(adapter=_BrokenProbeAdapter())) is False
    assert asyncio.run(validate_provider_connection(adapter=None)) is False
