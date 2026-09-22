# 보고서 생성 노드 — 설계서 E장 대응

이 문서는 `report` 노드의 프롬프트·rubric·조립 코드가 실습 가이드 C·E와 설계서 E.1~E.4를 어떻게 따르는지 기록합니다.
보고서 내용 책임자는 여전히 미정이며, 이 문서가 역할을 배정하지는 않습니다.

## 역할 분담: LLM이 쓰는 것과 코드가 만드는 것

설계서 E.3은 절마다 채워지는 State 키를 정합니다. 현재 구현은 다음과 같이 나눕니다.

| 절 | 내용 | 작성 주체 | 입력 |
| --- | --- | --- | --- |
| SUMMARY | 관점별 평가가 갈린 지점 중심 결론, 600자 이내 | LLM `summary` | `prior_results.synthesis` |
| 1. 분석 배경 | KV cache 병목, 두 진영, 도메인 선정 | 코드 (`prompts/report/sections.j2`) | `domain`, `target_techs`, 설계서 A.1·C.1 |
| 2. 기술 선정 | 선정 방식·사유, 탈락 후보, ITME 프리프린트 주의 | 코드 (`sections.j2`) | 설계서 A.2~A.4·B.1 |
| 3.1/3.2 기술 개요 | 원리·적용 조건·한계·경쟁 기술과의 관계 | LLM claims `criterion=overview` + 코드 표 3-1 | `tech_result`, `stakeholder_result.competitors` |
| 3.3 TRL | 잠정(기술 조사)·확정(평가 종합) 단계, 공개 정보 기반 추정 명시 | 코드 | `tech_result.maturity`, `trl_result` |
| 4.1~4.3 관점별 평가 | 서술 + 평가 노드 요약 + 두 기술 비교표 + 검증된 근거 문장 | LLM claims `market/stakeholder/domain` + 코드 표 | 각 `*_result` |
| 5. 시사점 | 5.1 상충 교차표·일치 상충, 5.2 조건부 시사점·보완 관계 | LLM claims `implications` + 코드 표 | `synthesis` |
| 6.1 정보의 한계 | 확인 불가, 출처 편향, 프리프린트, 해석 검토 대상 | LLM `limitations` + 코드 (`gaps`, 노드별 한계·미확인) | 전체 |
| 6.2 확증편향 방지 조치 | C.2 운용 원칙·C.7 점검·D.3 종료 규칙과 실행 사실 표 | 코드 (`sections.j2` + 검색·검증 이력) | 각 `NodeRun` |
| REFERENCE | 실제 인용 자료만, 가이드 표기 형식, 문서별 1항목 | 코드 | `sources` |

LLM 출력은 다른 노드와 같은 `NodeResult`입니다. 보고서 문장은 `claims`로 작성하고 `criterion`이 들어갈 절을 지정합니다.
모든 claim은 인용 Judge가 원문(evidence)과 대조하므로, 상위 노드의 판정 등급("높음", "조건부 적합")은 문장의 주장이 아니라 `rationale`에 "역할/기술/기준: 등급"으로 적습니다.

## rubric 항목과 판정

`rubrics/report.yaml`의 criterion은 보고서 절입니다: `overview`, `market`, `stakeholder`, `domain`, `implications`.
두 기술 × 5절 = 10문항으로 공통 질문 한도(10) 안에 들어갑니다.

기존 개인 report 입력의 `criterion=coverage`는 더 이상 사용하지 않습니다. 절별 새 ID로 질문·claims·assessments를 함께 이관하거나 최신 acceptance 입력에서 시작하세요. NodeInput/NodeResult 외피와 다른 노드의 criterion은 유지합니다. 이전 `구성 미충족` 대신 `부분 구성`을 사용하며 근거 자체가 없으면 `확인 불가`로 남깁니다.

판정은 기술 자체의 평가가 아니라 그 절의 **구성 상태**입니다.

- `구성 충족`: 필요한 상위 결과의 모든 항목이 판정·이유·근거를 갖췄고 결론·조건·한계·출처 연결이 claims에 유지됨.
- `부분 구성`: 일부 항목이 확인 불가·미검증이지만 나머지는 원문 근거로 옮겼고 누락 항목을 `unverified`에 "기술/절: 항목"으로 남김.
- `확인 불가`: 그 기술·절에 인용할 원문 근거나 상위 결과가 없음.

근거 부족을 낮은 등급으로 바꾸지 않는 공통 규칙은 그대로입니다.

## 가이드·설계서 준수 장치

- 우열·추천 금지: 프롬프트가 금지하고, `validate_report`가 SUMMARY와 보고서 문장에서 "추천한다", "더 우수", "우위에 있" 등의 표현을 검출합니다.
- SUMMARY 분량: 설계서 E.1의 1/2 페이지를 `SUMMARY_MAX_CHARS=600`으로 검사합니다.
- 출처 누적과 출력: 보완 이전 근거는 sources 이력에 보존하지만 문서별 REFERENCE 묶음에는 현재 결과가 실제 인용한 근거만 넣습니다. 오래된 미사용 근거의 서지 누락은 최종 검사를 막지 않습니다.
- SUMMARY 시작·REFERENCE 종료: 조립 순서가 고정이며 `validate_report`가 확인합니다.
- REFERENCE 형식: 논문 `저자(YYYY). 제목. 학술지/학회명. URL`, 웹 `기관명 또는 작성자(YYYY-MM-DD). 제목. 사이트명, URL`. 같은 문서의 여러 chunk는 하나의 항목으로 묶고 인용 ID·페이지를 뒤에 남깁니다. 특허 형식은 현재 Evidence 유형에 없어 미구현입니다.
- 자사/독립 표시: REFERENCE 항목마다 `[자사 자료]`, `[독립 자료]`, `[출처 관계 미분류]`를 붙이고, 미분류는 검사 문제로 남습니다.
- 수치 없는 고정 서술: 1·2장은 설계서 서술만 옮기고 성능·시장 수치를 넣지 않습니다. 수치는 근거 ID가 붙는 3~5장에만 나옵니다.
- 설계서 해석 차이 명시: C.2의 "애매하면 낮은 등급"은 확인 불가로 보류한다는 사실을 6.2에 적습니다. [최종 설계 점검](final-design-review.md)의 쟁점을 숨기지 않습니다.

## 실행과 확인

```bash
uv run python -m app.run_node --node report --mode mock --case acceptance
uv run python -m app.run_node --node report --mode fixture --case acceptance
uv run python -m app.run_pipeline --mode mock
uv run pytest tests/test_report_node.py -q
```

기본 fixture의 `prior_results`는 두 공유 발췌만 사용한 형식 예시라 대부분 확인 불가입니다.
프롬프트 품질을 보려면 실제 실행 `state.json`의 각 `result`를 `prior_results`에, 인용된 evidence를 `evidence`에 넣은 개인 입력을 `data/private/`에 만들어 `--input`으로 전달하세요.
전체 파이프라인에서는 `prior_results`가 상위 노드의 실제 결과로 자동 대체됩니다.

`report_check.ready`가 true여도 형식·인용 연결 검사일 뿐입니다. 등급과 판정 조건의 일치, 원문 인용의 정확성, 자료 편향, 상충 보존은 사람이 다시 확인해야 하며 mock 보고서는 제출물이 아닙니다.
