import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app, visits


class ApiTests(unittest.TestCase):
    def setUp(self):
        visits.clear()
        self.client = TestClient(app)

    def test_health(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')

    def test_prepare_contract(self):
        with patch('api.main.runtime.run', return_value={'status': 'unverified'}) as run:
            response = self.client.post('/api/v1/prepare', json={'hospital': '测试医院', 'service': '胃镜'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['status'], 'unverified')
        run.assert_called_once_with('测试医院', '胃镜')

    def test_validation(self):
        response = self.client.post('/api/v1/prepare', json={'hospital': '', 'service': '胃镜'})
        self.assertEqual(response.status_code, 422)

    def test_rate_limit(self):
        with patch('api.main.runtime.run', return_value={'status': 'unverified'}):
            responses = [self.client.post('/api/v1/prepare', json={'hospital': '测试医院', 'service': '胃镜'}) for _ in range(11)]
        self.assertEqual(responses[-1].status_code, 429)


if __name__ == '__main__':
    unittest.main()
