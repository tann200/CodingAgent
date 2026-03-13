import unittest
from unittest.mock import patch, mock_open
from src.adapters.ollama_adapter import OllamaAdapter
import json

class TestOllamaAdapterUnit(unittest.TestCase):
    @patch('pathlib.Path.read_text')
    @patch('builtins.open', new_callable=mock_open, read_data='{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}')
    def test_load_provider(self, mock_file, mock_read_text):
        mock_read_text.return_value = '{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}'
        adapter = OllamaAdapter('dummy_path')
        self.assertEqual(adapter.base_url, 'http://127.0.0.1:11434')
        self.assertEqual(adapter.models, ['qwen3.5:9b'])

    @patch('requests.get')
    @patch('pathlib.Path.read_text')
    @patch('builtins.open', new_callable=mock_open, read_data='{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}')
    def test_get_models_from_api(self, mock_file, mock_read_text, mock_requests):
        mock_read_text.return_value = '{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}'
        mock_requests.return_value.status_code = 200
        mock_requests.return_value.json.return_value = {"models": ["qwen3.5:9b", "other-model"]}
        adapter = OllamaAdapter('dummy_path')
        models = adapter.get_models_from_api()
        self.assertIn('models', models)
        self.assertIn('qwen3.5:9b', models['models'])

    @patch('pathlib.Path.write_text')
    @patch('requests.get')
    @patch('pathlib.Path.read_text')
    @patch('builtins.open', new_callable=mock_open, read_data='{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}')
    def test_update_models_list(self, mock_file, mock_read_text, mock_requests, mock_write_text):
        mock_read_text.return_value = '{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}'
        mock_requests.return_value.status_code = 200
        mock_requests.return_value.json.return_value = {"models": ["qwen3.5:9b", "other-model"]}
        adapter = OllamaAdapter('dummy_path')
        updated = adapter.update_models_list()
        self.assertEqual(updated, ["qwen3.5:9b", "other-model"])

if __name__ == '__main__':
    unittest.main()

