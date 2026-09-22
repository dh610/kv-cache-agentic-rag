# 시장 고정 근거 테스트 자료

이 디렉터리의 입력은 NodeInput 형식이며, 실제 기술 평가 결과나 검색 성능 측정 자료가 아닙니다.
합성 사례의 기관·제품·사건·수치는 모두 합성입니다. KIVI·ITME라는 평가 대상명만 재사용합니다.
모든 합성 근거는 source_type=fixture, 제목/본문의 SYNTHETIC 표기, example.invalid URL로 구분합니다.
실제 보고서의 출처나 최종 인수 근거로 사용하지 마세요.

## 사례와 기대 동작

모든 입력은 두 기술 × adoption/growth/ecosystem의 6개 질문을 포함합니다.
두 기술에 같은 가상 조건을 주어 기술 이름만으로 판정이 달라지는지 점검할 수 있습니다.

| 입력 파일 | 검토할 경계 | 기대 판정 (채택 / 성장 / 생태계, 두 기술 공통) |
| --- | --- | --- |
| supported.json | 별개의 실제 도입·정식 기능 출시, 전망 1출처 | 높음 / 보통 / 높음 |
| planned-only.json | 정식 제품 출시 계획과 실제 출시 구분 | 사람 검토 / 확인 불가 / 확인 불가 |
| context-only.json | 분야 전망과 선정 기술 채택 구분 | 확인 불가 / 보통 / 확인 불가 |
| duplicate-announcement.json | 같은 파일럿 발표의 재인용 | 보통 / 확인 불가 / 확인 불가 |
| conflicting-forecasts.json | 같은 시장·기간의 반대 전망 | 확인 불가 / 낮음 / 확인 불가 |
| similar-implementation.json | 같은 계열·영감 받은 구현과 원 기술 구분 | 확인 불가 / 확인 불가 / 확인 불가 |
| missing-metadata.json | 지원·버전·범위는 명확하나 날짜만 누락 | 보통 / 확인 불가 / 보통 |
| upstream-mismatch.json | 상위 요약과 원문 불일치 | 보통 / 확인 불가 / 보통 |
| missing-evidence.json | 원문 없이 6항목 처리 | 확인 불가 / 확인 불가 / 확인 불가 |
| two-independent-cases.json | 독립 도입 2건·전망 2출처 | 높음 / 높음 / 높음 |
| documented-low.json | 명시적인 낮음 근거와 정보 부족 구분 | 낮음 / 낮음 / 낮음 |
| standardization-discussion.json | 진행 중인 표준화와 공식 채택 구분 | 확인 불가 / 확인 불가 / 보통 |
| research-code-only.json | 논문·데모/원 저자 코드와 추가 근거 미확인 | 낮음 / 확인 불가 / 낮음 |

basic.json과 acceptance.json은 기존 짧은 논문 발췌를 사용하는 연결 점검 입력입니다.
supported.json은 별개의 실제 도입과 정식 기능 출시 2건, 전망 1출처인 사례입니다. two-independent-cases.json은 도입과 시장 전망 모두 높음 기준을 충족하는 사례입니다.
expectations.json은 사례별 기대 판정과 사람 검토 기준이며 NodeInput이 아닙니다.
Generator 입력이나 evidence에 정답 파일을 포함하지 마세요.

planned-only의 채택 기대값은 null로 표시하고 사람 검토 대상으로 분리했습니다. 설계서는 출시 계획의 하향 전 등급까지 정의하지 않으므로 임의의 등급 정답을 만들지 않습니다.
이 null은 검토 파일 전용이며 실제 NodeResult에는 허용되지 않습니다. 등급 정답률 계산에서는 제외하고 출시 여부 구분·한 등급 하향 사유·미확인 보존을 검토합니다.
basic에는 고정 기준일·이후 자료 차단 조건을 두지 않습니다. acceptance와 합성 사례의 날짜는 고정 테스트 조건입니다.

## 실행과 해석

저장소 루트에서 연결만 확인하려면 다음을 실행합니다.

```bash
uv run python -m app.run_node --node market --mode mock --input tests/fixtures/market/supported.json
uv run python -m app.run_node --node market --mode mock --case missing-evidence
```

mock은 rubric에 따라 추론하지 않습니다. 합성 근거를 읽었다는 사실과 실행 계약만 점검하며,
expectations.json의 의미적 정답을 맞히는 평가가 아닙니다.
특히 현재 MockBackend는 근거가 없으면 assessment를 생성하지 않으므로 missing-evidence mock은
needs_revision(종료 코드 2)이 정상입니다. 이것을 6개 확인 불가 응답의 생성 성공으로 해석하지 마세요.

실제 LLM 평가는 API 키 설정과 유료 호출에 대한 명시적 요청이 있을 때 수행합니다.
fixture 모드는 고정 근거를 쓰지만 Generator/Judge는 실제 API로 호출합니다.

```bash
uv run python -m app.run_node --node market --mode fixture --input tests/fixtures/market/planned-only.json
```

결과의 6개 판정뿐 아니라 원문 인용, 조건·수치 보존, 상충·미확인 기록을 expectations.json과 대조합니다.
unknown 항목이 있는 실제 실행은 needs_revision일 수 있으며 이것만으로 의미적 평가 실패는 아닙니다.
합성 자료의 최종 인수 거부는 정상이며, 인수 검사를 통과시키려고 source_type을 web/paper로 바꾸지 마세요.
이 단계에서는 실제 LLM 평가를 수행하지 않았습니다.

## 5단계: 저장 결과 자동 검사

`app/evaluate_market.py`는 이미 저장된 fixture 실행 결과를 검사하는 개발 도구입니다.
에이전트 그래프의 추가 노드나 설계서의 추가 판정 규칙이 아닙니다. LLM·검색 API를 호출하지 않습니다.
앞서 실제 fixture 실행으로 생성한 디렉터리를 다음 명령에 전달합니다.

```bash
uv run python -m app.evaluate_market --case supported --run outputs/local/RUN
uv run pytest tests/test_market_eval.py -q
```

`RUN`은 실제 실행 디렉터리로 바꿉니다. `input.json`과 `result.json`이 필요하며 입력 전체
(상위 결과 포함)와 근거가 선택한 고정 사례와 일치해야 합니다. mock 결과는 평가 대상으로 인정하지 않습니다.

- 기존 13개 사례의 고정 기대 등급 76개를 대조합니다. 사례별로 실행하며 새로운 정답을 만들지 않습니다.
- 두 기술 × 세 항목의 누락·중복, 허용 등급, 인용 ID·기술 일치, Judge 연결, 검색 이력,
  실패 상태와 확인 불가의 미확인 기록을 검사합니다. 합성 출처를 실제 보고서 인수 근거로 승인하지 않습니다.
- planned-only의 두 채택 항목은 자동 대조에서 제외하고 `manual_items`에 이유와 함께 반환합니다.
- `automatic_decision=pass`는 자동 검사 통과일 뿐입니다. 수동 등급 항목이 있으면 `inconclusive`,
  자동 오류가 있으면 `fail`입니다. 종료 코드는 각각 0, 2, 2이며 입력 오류는 1입니다.
- 모든 결과에 `human_review_required=true`와 기존 `review_checks`를 반환합니다.
  인용 내용의 의미, 수치·조건 보존, 이해당사자 표시, 상충·미확인의 충분한 설명은 사람이 검토합니다.

`tests/test_market_eval.py`는 인위적으로 만든 정상·오류 결과로 **검증기 자체**를 테스트합니다.
이 테스트 통과는 실제 모델이 76개 기대 판정을 맞혔다는 뜻이 아닙니다. 실제 LLM 준수 평가는 별도입니다.

보조 문서(`document_role=reference`)나 분야 배경(`scope=context`)은 평가 대상과 기술명이
달라도 일괄 거부하지 않습니다. 다른 기술로 분류된 해당 인용은
`contextual_citations_for_review`에 반환합니다. 이는 인용의 의미가 타당하다는 자동 승인이 아니며,
사람이 관련성과 분야/선정 기술의 구분을 확인해야 합니다. 그 외 다른 기술의 직접 근거를 잘못
인용한 경우, 기대 등급 위반, 알 수 없는 ID, Judge 검증 실패는 계속 오류로 처리합니다.

기본 실행과 인수 입력의 질문에는 긍정·비판 자료를 모두 찾도록 요구했습니다.
fixture는 고정 근거만 제공하므로 이 요구의 실제 검색 수행을 검증하지 않습니다.
양쪽 검색 실행·기록과 한쪽 자료만 확보된 경우의 기록은 검색 담당과 통합하여 확인해야 합니다.

## 계약 v2 이관

공통 런타임 v2의 planner·충분성 검사·verdict·최대 1회 답변 수정·재검증을 사용합니다.
검증기는 질문별 coverage, fixture 검색 intent, fix_count, verdict와 상태의 일관성도 확인합니다.
이전 v1 결과 JSON은 재사용하지 말고 v2에서 다시 실행합니다. 시장은 trl_estimates를 비워 둡니다.
출처 affiliation/affiliation_reason/stance는 공통 스키마로 읽고 보존합니다. 기존 합성 자료에
명시되지 않은 분류는 기본값 unknown으로 유지하며, 기술명이나 검색 의도로 분류를 추정하지 않습니다.

공통 실제 검색에서는 positive/critical intent를 기록하고 양쪽 검색 누락을 gaps에 남깁니다.
fixture는 여전히 질문당 한 번, intent=fixture로 실행하므로 실제 양쪽 검색의 대체 검증이 아닙니다.
시장 질문의 검색 요구와 v2 실행 구조는 연결했지만 실제 API 품질·원문 입장 분류의 사람 검토는 남아 있습니다.
