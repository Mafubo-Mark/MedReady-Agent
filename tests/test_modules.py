import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.harness_compliance import check_input, inject_disclaimer
from core.harness_runtime import MedReadyAgentRuntime, plan_task, search_failure_message
from core.harness_state import Status, TaskState
from core.harness_llm import MedReadyLLMHarness
from core.harness_tool import BaseTool, ToolRegistry
from core.tools.html_extractor import HtmlExtractorTool, extract_text, extract_title, public_http_url
from core.tools.search_baidu import BaiduSearchTool, build_queries, rank_results
from utils.json_validator import UNKNOWN, validate_result
from utils import cache


SOURCE = {'title': '北京协和医院 胃镜检查须知', 'url': 'https://example.org/notice',
          'snippet': '北京协和医院 胃镜检查请携带身份证。', 'publisher': '北京协和医院',
          'date': '2026-09-01'}


class FakeSearch(BaseTool):
    name = 'search_baidu'
    def run(self, params):
        return [SOURCE]


class FakeExtract(BaseTool):
    name = 'html_extractor'
    def run(self, params):
        return {'url': params['url'], 'text': '北京协和医院胃镜检查须知。检查请携带身份证。' * 5}


class FakeLLM:
    def chat(self, system_prompt, user_prompt, json_mode=True):
        assert 'SOURCE 1' in user_prompt
        return json.dumps({'fields': {
            'materials_list': {'value': ['身份证'], 'source_id': 1, 'quote': '检查请携带身份证。'},
            'estimated_cost': {'value': '500元', 'source_id': 1, 'quote': '不存在的费用'},
        }}, ensure_ascii=False)


class ModuleTests(unittest.TestCase):
    def test_blood_test_fasting_is_not_endoscopy_preparation(self):
        quote = '在工作日上午11:00前完成空腹抽血检查。'
        source = {**SOURCE, 'text': quote}
        result = validate_result({'fields': {'fasting_requirement': {
            'value': quote, 'source_id': 1, 'quote': quote}}}, [source], '北京协和医院', '胃镜')
        self.assertIsNone(result.fields['fasting_requirement'].source_id)

    def test_compliance(self):
        self.assertFalse(check_input('胃疼吃什么药')[0])
        self.assertTrue(check_input('北京协和医院 胃镜')[0])
        self.assertIn('disclaimer', inject_disclaimer({}))

    def test_state_and_plan(self):
        state = TaskState('北京协和医院', '胃镜')
        state.update_status(Status.SEARCHING)
        state.append_context('abc')
        self.assertEqual(state.status, Status.SEARCHING)
        self.assertIn('abc', state.get_context())
        self.assertEqual(len(plan_task(state.hospital, state.service).queries), 3)

    def test_open_web_search(self):
        self.assertNotIn('site:', ' '.join(build_queries('北京协和医院', '胃镜')))
        items = rank_results([{'title': '北京协和医院 胃镜', 'url': 'https://pumch.example/info',
                               'content': '检查须知'}], '北京协和医院', '胃镜')
        self.assertEqual(len(items), 1)

    def test_baidu_request_shape(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'references': [{'title': '北京协和医院 胃镜', 'url': 'https://pumch.example/info', 'content': '检查须知'}]}
        class Session:
            def __init__(self): self.calls = []
            def post(self, *args, **kwargs):
                self.calls.append(kwargs)
                return Response()
        session = Session()
        output = BaiduSearchTool(api_key='Bearer test-only', session=session).run({'hospital': '北京协和医院', 'service': '胃镜'})
        self.assertEqual(len(session.calls), 3)
        self.assertNotIn('search_filter', session.calls[0]['json'])
        self.assertEqual(session.calls[0]['headers']['X-Appbuilder-Authorization'], 'Bearer test-only')
        self.assertEqual(len(output), 1)

    def test_html_extraction_and_private_url(self):
        self.assertEqual(extract_text('<html><nav>Menu</nav><article>医院检查须知</article></html>'), '医院检查须知')
        self.assertEqual(extract_title('<title>滨州市人民医院</title><h1>分享到微信</h1>'), '滨州市人民医院')
        self.assertFalse(public_http_url('http://127.0.0.1/private'))
        self.assertFalse(public_http_url('file:///etc/passwd'))

    def test_extractor_detects_utf8_from_html_bytes(self):
        class Response:
            status_code = 200
            headers = {'Content-Type': 'text/html'}
            content = ('<meta charset="utf-8"><h1>消化内科</h1>'
                       '<article>就诊预约方式：微信预约。</article>').encode('utf-8')
            text = content.decode('iso-8859-1')
            def raise_for_status(self): pass
        class Session:
            def get(self, *args, **kwargs): return Response()
        with patch('core.tools.html_extractor.public_http_url', return_value=True):
            result = HtmlExtractorTool(session=Session()).run({'url': 'https://example.org/hospital'})
        self.assertEqual(result['title'], '消化内科')
        self.assertIn('就诊预约方式', result['text'])

    def test_evidence_validation(self):
        source = {**SOURCE, 'text': '检查请携带身份证。'}
        fields = {'materials_list': {'value': ['身份证'], 'source_id': 1, 'quote': '检查请携带身份证。'},
                  'estimated_cost': {'value': '500元', 'source_id': 1, 'quote': '不在页面中的文字'}}
        result = validate_result({'fields': fields}, [source], '北京协和医院', '胃镜')
        self.assertEqual(result.fields['materials_list'].value, ['身份证'])
        self.assertEqual(result.fields['estimated_cost'].value, UNKNOWN)

    def test_guidance_outranks_biographies_and_duplicate_footers(self):
        hospital = '北京协和医院'
        items = [
            {'title': '张医生', 'url': 'https://hospital.example/doc/1', 'website': hospital,
             'content': '北京协和医院 胃镜 擅长 详细介绍 毕业于 医学院'},
            {'title': '就诊攻略', 'url': 'https://hospital.example/guide', 'website': hospital,
             'content': '内镜中心 胃肠镜预约流程，请携带预约单并前往内镜中心报到。'},
            {'title': '国际医疗部简介', 'url': 'https://hospital.example/a', 'website': hospital,
             'content': '北京协和医院 国际医疗部预约电话 地址 请使用官方APP查询预约挂号。' * 3},
            {'title': '国际医疗部简介', 'url': 'https://hospital.example/b', 'website': hospital,
             'content': '北京协和医院 国际医疗部预约电话 地址 请使用官方APP查询预约挂号。' * 3},
            {'title': '胃镜注意事项', 'url': 'https://directory.example/qa/1', 'website': '医疗问答',
             'content': '作者在北京协和医院工作。这是一般性医学问答。'},
        ]
        ranked = rank_results(items, hospital, '胃镜')
        self.assertEqual(ranked[0]['url'], 'https://hospital.example/guide')
        self.assertEqual(sum(x['title'] == '国际医疗部简介' for x in ranked), 1)
        self.assertFalse(any('directory.example' in x['url'] for x in ranked))

    def test_scope_and_medication_output_guard(self):
        source = {**SOURCE, 'title': '国际医疗部（东单院区）就诊指南',
                  'text': '到五层内镜中心预约。检查前停服抗凝药。'}
        result = validate_result({'fields': {
            'appointment_process': {'value': '到五层内镜中心预约。', 'source_id': 1, 'quote': '到五层内镜中心预约。'},
            'attention_points': {'value': '检查前停服抗凝药。', 'source_id': 1, 'quote': '检查前停服抗凝药。'},
        }}, [source], '北京协和医院', '胃镜')
        self.assertIn('国际医疗部', result.fields['appointment_process'].scope)
        self.assertIn('东单院区', result.fields['appointment_process'].scope)
        self.assertIsNone(result.fields['attention_points'].source_id)

    def test_official_appointment_backup_and_whitespace_evidence(self):
        source = {'title': '消化内科', 'url': 'https://example.org/notice',
                  'publisher': '滨州市人民医院', 'text': '消化内科\n就诊预约方式：\n微信可关注“滨州市人民医院掌上医院”预约消化内科门诊。\n'}
        result = validate_result({'fields': {
            'materials_list': {'value': ['证件'], 'source_id': 1, 'quote': '消化内科 就诊预约方式：'}
        }}, [source], '滨州市人民医院', '消化内科')
        self.assertIn('预约消化内科门诊', result.fields['appointment_process'].value)
        self.assertEqual(result.fields['appointment_process'].source_id, 1)
        self.assertEqual(len(result.sources), 1)

    def test_runtime_with_mocked_services(self):
        registry = ToolRegistry()
        registry.register(FakeSearch())
        registry.register(FakeExtract())
        result = MedReadyAgentRuntime(registry=registry, llm=FakeLLM(), use_cache=False).run('北京协和医院', '胃镜')
        self.assertEqual(result['status'], 'verified_sources')
        self.assertEqual(result['fields']['materials_list']['value'], ['身份证'])
        self.assertEqual(result['fields']['estimated_cost']['value'], UNKNOWN)
        self.assertEqual(len(result['sources']), 1)

    def test_binzho_department_prefers_hospital_source(self):
        hospital = '滨州市人民医院'
        official = {'title': '分享到微信', 'url': 'https://www.bzrmyy.com.cn/html/zdxk1/20230616/6404.html',
                    'snippet': '滨州市人民医院消化内科 就诊预约方式', 'publisher': hospital, 'date': '2023-06-16'}
        unrelated = {'title': '医生预约挂号', 'url': 'https://m.bohe.cn/doctor/voice/6720404.html',
                     'snippet': '滨州市人民医院 消化内科', 'publisher': '无', 'date': ''}
        class Search(BaseTool):
            name = 'search_baidu'
            def run(self, params): return [official, unrelated]
        class Extract(BaseTool):
            name = 'html_extractor'
            def run(self, params):
                return {'title': '消化内科', 'text': '消化内科\n就诊预约方式：\n微信可关注“滨州市人民医院掌上医院”；电话可拨打3284114预约消化内科门诊。' * 3}
        class EmptyLLM:
            def chat(self, *args, **kwargs): return '{"fields":{}}'
        registry = ToolRegistry()
        registry.register(Search())
        registry.register(Extract())
        result = MedReadyAgentRuntime(registry=registry, llm=EmptyLLM(), use_cache=False).run(hospital, '消化内科')
        self.assertEqual(result['status'], 'verified_sources')
        self.assertIn('3284114', result['fields']['appointment_process']['value'])
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(result['sources'][0]['title'], '消化内科')

    def test_runtime_search_failure(self):
        class BrokenSearch(BaseTool):
            name = 'search_baidu'
            def run(self, params): raise RuntimeError('offline')
        registry = ToolRegistry()
        registry.register(BrokenSearch())
        result = MedReadyAgentRuntime(registry=registry, llm=FakeLLM(), use_cache=False).run('北京协和医院', '胃镜')
        self.assertEqual(result['status'], 'unverified')
        self.assertIn('搜索服务', result['message'])

    def test_search_failure_messages(self):
        self.assertIn('鉴权失败', search_failure_message('401 Client Error'))
        self.assertIn('调用次数', search_failure_message('429 Client Error'))
        self.assertIn('密钥', search_failure_message('BAIDU_SEARCH_API_KEY is not configured'))

    def test_llm_json_request(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"fields":{}}'}}]}
        class Session:
            def post(self, url, **kwargs):
                self.url, self.kwargs = url, kwargs
                return Response()
        session = Session()
        result = MedReadyLLMHarness(api_key='test-only', session=session).chat('Return JSON', 'test')
        self.assertEqual(result, '{"fields":{}}')
        self.assertEqual(session.kwargs['json']['response_format'], {'type': 'json_object'})

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cache, 'CACHE_DIR', Path(folder)):
            key = cache.make_key('医院', '胃镜')
            cache.set_cache(key, {'ok': True}, ttl=10)
            self.assertEqual(cache.get_cache(key), {'ok': True})
            cache.set_cache(key, {'ok': True}, ttl=-1)
            self.assertIsNone(cache.get_cache(key))


if __name__ == '__main__':
    unittest.main()
