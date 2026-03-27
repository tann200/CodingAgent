import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os
from src.core.inference.adapters.ollama_adapter import OllamaAdapter


class IntegrationTestOllamaAdapter(unittest.TestCase):
    def setUp(self):
        # Use a temp file so tests don't mutate the real providers.json
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        json.dump(
            [{"name": "ollama", "type": "ollama", "base_url": "http://localhost:11434", "models": []}],
            self._tmp,
        )
        self._tmp.close()
        self.config_path = self._tmp.name
        self.adapter = OllamaAdapter(self.config_path)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    @patch('requests.get')
    def test_get_models_from_api(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}
        mock_get.return_value = mock_response

        models = self.adapter.get_models_from_api()
        self.assertIn('models', models)
        self.assertTrue(len(models['models']) > 0)

    @patch('requests.get')
    def test_update_models_list(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}
        mock_get.return_value = mock_response

        updated_models = self.adapter.update_models_list()
        self.assertTrue(isinstance(updated_models, list))
        self.assertTrue(len(updated_models) > 0)

    @patch('requests.get')
    def test_update_models_preserves_array_format(self, mock_get):
        """update_models_list must not revert providers.json from array to dict."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"name": "llama3"}]}
        mock_get.return_value = mock_response

        self.adapter.update_models_list()

        raw = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
        self.assertIsInstance(raw, list, "providers.json must remain an array after update_models_list")


if __name__ == '__main__':
    unittest.main()
