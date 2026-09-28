import re

DISCLAIMER_ZH = '本工具仅提供就诊流程信息，不构成医疗建议。具体要求请以医院官方通知或现场说明为准。'
DISCLAIMER_EN = 'This tool provides visit-process information only, not medical advice. Confirm requirements with the hospital.'

# Detect explicit medical-advice requests; procedure names alone remain allowed.
MEDICAL_REQUESTS = [
    r'(?:吃|服|用|开|推荐).{0,8}(?:什么药|哪种药|药物|处方)',
    r'(?:我|本人|孩子).{0,20}(?:得了什么病|是不是.*病|该怎么治|如何治疗)',
    r'(?:帮我|请|能否).{0,8}(?:诊断|开药|制定治疗方案)',
    r'\b(?:diagnose me|what medication should i take|prescribe|treatment plan for me)\b',
]


def check_input(text: str) -> tuple[bool, str]:
    if any(re.search(pattern, text, re.I) for pattern in MEDICAL_REQUESTS):
        return False, '仅支持就诊流程和准备信息查询；诊断、用药或治疗问题请咨询医生。'
    return True, ''


def inject_disclaimer(result: dict) -> dict:
    return {**result, 'disclaimer': DISCLAIMER_ZH, 'disclaimer_en': DISCLAIMER_EN}


def check_output(value) -> bool:
    """Do not publish medicine instructions even when they occur in a source."""
    text = str(value)
    return not re.search(r'停药|停服|停用|抗凝|降压药|药物剂量|服药剂量|\b(?:stop taking|dosage|anticoagulant)\b', text, re.I)
