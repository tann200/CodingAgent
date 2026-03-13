import os
import unittest
from unittest.mock import patch, MagicMock
from src.adapters.ollama_adapter import OllamaAdapter

class IntegrationTestOllamaAdapter(unittest.TestCase):
    def setUp(self):
        self.config_path = os.path.join(os.path.dirname(__file__), '../../src/config/providers.json')
        self.adapter = OllamaAdapter(self.config_path)

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

if __name__ == '__main__':
    unittest.main()

