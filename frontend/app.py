import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API_BASE = os.getenv('API_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
LABELS = {
    'fasting_requirement': '空腹与饮食要求',
    'materials_list': '携带材料',
    'appointment_process': '预约与取号',
    'department_location': '科室与地点',
    'estimated_cost': '费用',
    'insurance_tips': '医保',
    'attention_points': '其他注意事项',
}

st.set_page_config(page_title='诊前智备 MedReady Agent', page_icon='🏥', layout='centered')
st.title('诊前智备 MedReady Agent')
st.write('查询医院公开发布的就诊流程与检查准备信息，并查看每项信息的出处。')
st.info('具体要求请以医院官方通知或现场说明为准。请勿输入姓名、身份证号或病历。')

with st.form('query'):
    hospital = st.text_input('医院名称', placeholder='例如：北京协和医院')
    service = st.text_input('检查或科室项目', placeholder='例如：胃镜')
    submitted = st.form_submit_button('查询准备信息', type='primary')

if submitted:
    if len(hospital.strip()) < 2 or len(service.strip()) < 2:
        st.warning('请输入完整的医院名称和项目。')
    else:
        try:
            with st.spinner('正在检索并核对公开资料…'):
                response = requests.post(f'{API_BASE}/api/v1/prepare',
                                         json={'hospital': hospital.strip(), 'service': service.strip()},
                                         timeout=180)
                response.raise_for_status()
                result = response.json()['data']
            st.subheader(f"{result['hospital']} · {result['service']}")
            if result.get('message'):
                st.warning(result['message'])
            fields = result.get('fields') or {}
            supported = {key: value for key, value in fields.items() if value.get('source_id')}
            if not supported and not result.get('message'):
                st.warning('本次未能核对具体要求，请向医院确认。')
            for key, label in LABELS.items():
                entry = supported.get(key)
                if not entry:
                    continue
                value = entry.get('value') or '暂无明确说明，请向医院确认。'
                with st.container(border=True):
                    st.markdown(f'**{label}**')
                    if entry.get('scope'):
                        st.caption(f"适用范围：{entry['scope']}")
                    if isinstance(value, list):
                        for item in value:
                            st.write(f'• {item}')
                    else:
                        st.write(value)
                    if entry.get('source_id'):
                        st.caption(f"来源 {entry['source_id']} · 原文：{entry.get('quote', '')}")
            missing = [label for key, label in LABELS.items() if key not in supported]
            if missing:
                st.info('尚未核对：' + '、'.join(missing) + '。请按医院为您开具的导诊单或预约通知确认。')
            sources = result.get('sources') or []
            if sources:
                st.subheader('资料来源')
                for source in sources:
                    st.markdown(f"{source['id']}. [{source['title'] or source['url']}]({source['url']})")
                    details = ' · '.join(x for x in (source.get('publisher'), source.get('date'),
                                                   '搜索摘要' if source.get('evidence_type') == 'search snippet' else '网页正文') if x)
                    if details:
                        st.caption(details)
            if result.get('reference_pages'):
                st.subheader('可直接查看的网页')
                for source in result['reference_pages']:
                    st.link_button(source['title'], source['url'])
            st.caption(result.get('disclaimer', ''))
        except (requests.RequestException, KeyError, ValueError) as exc:
            st.error('暂时无法连接服务或处理结果。请确认 API 已启动，然后重试。')
