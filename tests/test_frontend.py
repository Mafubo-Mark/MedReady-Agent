import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from streamlit.testing.v1 import AppTest


class FrontendTests(unittest.TestCase):
    def test_page_loads_with_expected_inputs(self):
        page = Path(__file__).resolve().parents[1] / 'frontend' / 'app.py'
        app = AppTest.from_file(str(page)).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, '诊前智备 MedReady Agent')
        self.assertEqual(len(app.text_input), 2)

    def test_partial_result_shows_supported_card_and_groups_unknowns(self):
        result = {'hospital': '北京协和医院', 'service': '胃镜', 'status': 'verified_sources',
                  'fields': {'appointment_process': {'value': '先预约门诊。', 'source_id': 1,
                             'quote': '先预约门诊。', 'scope': '国际医疗部 · 东单院区'}},
                  'sources': [], 'disclaimer': '测试说明'}
        response = Mock()
        response.json.return_value = {'data': result}
        page = Path(__file__).resolve().parents[1] / 'frontend' / 'app.py'
        app = AppTest.from_file(str(page)).run()
        app.text_input[0].set_value('北京协和医院')
        app.text_input[1].set_value('胃镜')
        with patch('requests.post', return_value=response):
            app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('预约与取号' in x.value for x in app.markdown))
        self.assertFalse(any(x.value == '**空腹与饮食要求**' for x in app.markdown))
        self.assertTrue(any('国际医疗部' in x.value for x in app.caption))
        self.assertTrue(any('尚未核对' in x.value for x in app.info))


if __name__ == '__main__':
    unittest.main()
