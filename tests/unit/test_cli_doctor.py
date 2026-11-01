"""Unit tests for ``codingagent doctor`` CLI subcommand."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.cli import doctor_cmd
from src.main import _parse_args


# --------------------------------------------------------------------------
# Argparse Parsing Tests
# --------------------------------------------------------------------------


class TestDoctorArgParser:
    def test_doctor_defaults(self):
        args = _parse_args(["doctor"])
        assert args.subcommand == "doctor"
        assert args.provider is None
        assert args.model is None
        assert args.endpoint is None
        assert args.validate_tools is False
        assert args.json is False

    def test_doctor_flags(self):
        args = _parse_args(
            [
                "doctor",
                "--provider",
                "ollama",
                "--model",
                "qwen3.5:9b",
                "--endpoint",
                "http://localhost:11434",
                "--validate-tools",
                "--json",
            ]
        )
        assert args.subcommand == "doctor"
        assert args.provider == "ollama"
        assert args.model == "qwen3.5:9b"
        assert args.endpoint == "http://localhost:11434"
        assert args.validate_tools is True
        assert args.json is True


# --------------------------------------------------------------------------
# Endpoint Reachability Tests
# --------------------------------------------------------------------------


class TestEndpointReachability:
    def test_empty_url(self):
        online, latency, err = doctor_cmd.check_endpoint_reachability("")
        assert online is False
        assert latency is None
        assert "Empty endpoint" in str(err)

    @patch("requests.get")
    def test_endpoint_online(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        online, latency, err = doctor_cmd.check_endpoint_reachability("http://localhost:11434")
        assert online is True
        assert isinstance(latency, float)
        assert err is None

    @patch("requests.get")
    def test_endpoint_connection_refused(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        online, latency, err = doctor_cmd.check_endpoint_reachability("http://localhost:11434")
        assert online is False
        assert latency is None
        assert "Connection refused" in str(err)

    @patch("requests.get")
    def test_endpoint_timeout(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("Timed out")

        online, latency, err = doctor_cmd.check_endpoint_reachability("http://localhost:11434", timeout=0.5)
        assert online is False
        assert latency is None
        assert "timed out" in str(err).lower()


# --------------------------------------------------------------------------
# Tool Schema Validation Tests
# --------------------------------------------------------------------------


class TestToolSchemaValidation:
    def test_flat_toolsets_valid(self):
        for ts_name in ["compact-flat", "standard-flat", "frontier-flat"]:
            schemas = doctor_cmd.get_tool_schemas_for_tier(ts_name)
            assert len(schemas) > 0, f"Expected schemas for {ts_name}"
            valid, errors = doctor_cmd.validate_tool_schemas(schemas)
            assert valid is True, f"Validation errors for {ts_name}: {errors}"
            assert len(errors) == 0

    def test_invalid_schema_caught(self):
        bad_schemas = [
            {"type": "other"},  # wrong type
            {"type": "function", "function": {"name": ""}},  # empty name
            {"type": "function", "function": {"name": "test", "description": ""}},  # empty description
            {
                "type": "function",
                "function": {
                    "name": "test2",
                    "description": "desc",
                    "parameters": {"type": "object", "properties": {"arg": {}}},  # missing type
                },
            },
        ]
        valid, errors = doctor_cmd.validate_tool_schemas(bad_schemas)
        assert valid is False
        assert len(errors) >= 3


# --------------------------------------------------------------------------
# Native Tool Acceptance Testing
# --------------------------------------------------------------------------


class TestNativeToolAcceptance:
    @patch("requests.post")
    def test_ollama_acceptance_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        schemas = doctor_cmd.get_tool_schemas_for_tier("compact-flat")[:2]
        ok, err = doctor_cmd.test_server_native_tool_acceptance(
            url="http://localhost:11434",
            provider_type="ollama",
            model="qwen3.5:9b",
            schemas=schemas,
        )
        assert ok is True
        assert err is None
        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "http://localhost:11434/api/chat"

    @patch("requests.post")
    def test_openai_compat_acceptance_rejected(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": {"message": "tools parameter not supported by loaded GGUF"}}
        mock_post.return_value = mock_resp

        schemas = doctor_cmd.get_tool_schemas_for_tier("compact-flat")[:2]
        ok, err = doctor_cmd.test_server_native_tool_acceptance(
            url="http://localhost:1234/v1",
            provider_type="lm_studio",
            model="qwen/qwen3.5-9b",
            schemas=schemas,
        )
        assert ok is False
        assert "tools parameter not supported" in str(err)


# --------------------------------------------------------------------------
# Provider Diagnostic Tests
# --------------------------------------------------------------------------


class TestProviderDiagnosis:
    def test_ollama_offline_diagnosis(self):
        with patch.object(doctor_cmd, "check_endpoint_reachability", return_value=(False, None, "Connection refused")):
            diag = doctor_cmd.diagnose_ollama_provider(
                provider_cfg={"name": "ollama", "base_url": "http://localhost:11434"}
            )
            assert diag["status"] == "offline"
            assert diag["online"] is False
            assert any("ollama serve" in r for r in diag["recommendations"])

    def test_ollama_online_compact_model_resolved(self):
        with patch.object(doctor_cmd, "check_endpoint_reachability", return_value=(True, 12.5, None)), \
             patch("requests.get") as mock_get:
            mock_tags_resp = MagicMock()
            mock_tags_resp.status_code = 200
            mock_tags_resp.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}
            mock_get.return_value = mock_tags_resp

            diag = doctor_cmd.diagnose_ollama_provider(
                provider_cfg={"name": "ollama", "base_url": "http://localhost:11434"},
                target_model="qwen3.5:9b",
            )
            assert diag["status"] in ("healthy", "warning")
            assert diag["online"] is True
            assert diag["latency_ms"] == 12.5
            assert diag["target_model"] == "qwen3.5:9b"
            assert diag["policy"]["tier"] == "compact"
            assert diag["policy"]["toolset"] == "compact-flat"
            assert diag["policy"]["tool_limit"] == 12
            assert diag["tool_validation"]["schemas_valid"] is True

    def test_lm_studio_online_diagnosis(self):
        with patch.object(doctor_cmd, "check_endpoint_reachability", return_value=(True, 8.4, None)), \
             patch("requests.get") as mock_get:
            mock_models_resp = MagicMock()
            mock_models_resp.status_code = 200
            mock_models_resp.json.return_value = {"data": [{"id": "qwen/qwen3.5-9b"}]}
            mock_get.return_value = mock_models_resp

            diag = doctor_cmd.diagnose_openai_compat_provider(
                provider_name="lm_studio",
                provider_cfg={"name": "lm_studio", "base_url": "http://localhost:1234/v1"},
                target_model="qwen/qwen3.5-9b",
            )
            assert diag["status"] == "healthy"
            assert diag["online"] is True
            assert diag["target_model"] == "qwen/qwen3.5-9b"
            assert diag["policy"]["tier"] == "compact"
            assert diag["policy"]["toolset"] == "compact-flat"
            assert diag["policy"]["tool_limit"] == 12


# --------------------------------------------------------------------------
# CLI End-to-End Execution Tests
# --------------------------------------------------------------------------


class TestDoctorExecution:
    def test_run_doctor_json(self, capsys):
        code = doctor_cmd.run_doctor(_parse_args(["doctor", "--json"]))
        out = capsys.readouterr().out
        # May contain sandbox warning lines before the JSON
        json_start = out.find("{")
        assert json_start != -1
        payload = json.loads(out[json_start:])
        assert "status" in payload
        assert "hardware" in payload
        assert "providers" in payload
        assert isinstance(payload["providers"], list)
        assert "recommendations" in payload

    def test_run_doctor_pretty(self, capsys):
        code = doctor_cmd.run_doctor(_parse_args(["doctor"]))
        out = capsys.readouterr().out
        assert "CodingAgent Doctor" in out
        assert "Hardware & Memory Capacity" in out
        assert "Sandbox Security" in out
        assert "Model Providers & 2026 Flat Toolsets" in out
