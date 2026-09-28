from typing import Any
import re

from pydantic import BaseModel, Field
from core.harness_compliance import check_output

UNKNOWN = '暂无明确说明，请向医院确认。'
FIELDS = ('fasting_requirement', 'materials_list', 'appointment_process',
          'department_location', 'estimated_cost', 'insurance_tips', 'attention_points')


class EvidenceField(BaseModel):
    value: Any = UNKNOWN
    source_id: int | None = None
    quote: str = ''
    scope: str = ''


class PreparationResult(BaseModel):
    hospital: str
    service: str
    fields: dict[str, EvidenceField] = Field(default_factory=dict)
    sources: list[dict] = Field(default_factory=list)
    disclaimer: str = ''
    disclaimer_en: str = ''
    status: str = 'unverified'


def validate_result(data: dict, sources: list[dict], hospital: str, service: str) -> PreparationResult:
    raw_fields = (data.get('fields', data) if isinstance(data, dict) else {})
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    source_map = {i + 1: s for i, s in enumerate(sources)}
    cleaned = {}
    for name in FIELDS:
        try:
            entry = EvidenceField.model_validate(raw_fields.get(name) or {})
        except Exception:
            entry = EvidenceField()
        source = source_map.get(entry.source_id)
        quote = entry.quote.strip()
        corpus = (source.get('text') or '') if source else ''
        normalized_quote = re.sub(r'\s+', '', quote)
        normalized_corpus = re.sub(r'\s+', '', corpus)
        # Blood-test fasting is not evidence for the requested endoscopy's fasting rules.
        unrelated_fasting = (name == 'fasting_requirement' and '镜' in service
                             and any(word in quote for word in ('抽血', '采血'))
                             and not any(word in quote for word in ('胃镜', '肠镜', '内镜')))
        # Unsupported or uncited assertions are never displayed as facts.
        if (unrelated_fasting or not source or not quote or normalized_quote not in normalized_corpus or not entry.value
                or not check_output(entry.value) or not check_output(entry.quote)):
            entry = EvidenceField()
        elif source:
            title = source.get('title', '')
            scopes = [word for word in ('国际医疗部', '特需', '住院') if word in title]
            scopes += re.findall(r'([\u4e00-\u9fff]{2,8}院区)', title)
            entry.scope = ' · '.join(scopes)
        cleaned[name] = entry
    # A narrow deterministic backup for an explicitly labelled appointment section.
    # This quotes the hospital's own line verbatim and never guesses other fields.
    if cleaned['appointment_process'].source_id is None:
        for source_id, source in source_map.items():
            if hospital not in source.get('publisher', ''):
                continue
            lines = (source.get('text') or '').splitlines()
            for index, line in enumerate(lines):
                if '就诊预约方式' not in line and '预约挂号' not in line:
                    continue
                for candidate in lines[index + 1:index + 5]:
                    candidate = candidate.strip()
                    if 12 <= len(candidate) <= 220 and any(
                        word in candidate for word in ('预约', '微信', '电话', '小程序', '自助机')
                    ):
                        cleaned['appointment_process'] = EvidenceField(
                            value=candidate, source_id=source_id, quote=candidate)
                        break
                if cleaned['appointment_process'].source_id is not None:
                    break
            if cleaned['appointment_process'].source_id is not None:
                break
    used_ids = {entry.source_id for entry in cleaned.values() if entry.source_id}
    public_sources = [{'id': i, 'title': s['title'], 'url': s['url'],
                       'publisher': s.get('publisher', ''), 'date': s.get('date', ''),
                       'evidence_type': s.get('evidence_type', 'page')}
                      for i, s in source_map.items() if i in used_ids]
    status = 'verified_sources' if any(x.source_id for x in cleaned.values()) else 'unverified'
    return PreparationResult(hospital=hospital, service=service, fields=cleaned,
                             sources=public_sources, status=status)
