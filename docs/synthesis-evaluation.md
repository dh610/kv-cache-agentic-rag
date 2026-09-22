# 평가 종합 노드 점검과 회귀 사례

`synthesis` 노드의 입출력, 적용 기준, 종합 전용 검사, 회귀 사례를 정리합니다. 이 문서는 구현 상태를 설명하며, mock 출력이나 고정 입력 결과를 실제 기술 평가로 간주하지 않습니다. 담당자 배정은 [협업 계약](team-contract.md#4-수정-범위와-역할-분담)의 팀 결정을 따르며, 이 작업은 사용자가 명시적으로 맡긴 범위입니다.

## 1. 상위에서 받는 것과 하위에 넘기는 것

| 방향 | 항목 | 실제 경로 | 확인 결과 |
| --- | --- | --- | --- |
| 입력 | 네 관점의 `NodeResult` | `graph/main_graph.py`가 `prior_results={tech, market, stakeholder, domain}`로 주입 | 제공됨. 단독 실행(`app.run_node`)에서는 fixture의 `prior_results`를 사용 |
| 입력 | 관점별 기술 사실·실험 조건·한계 | `prior_results[role].claims[].conditions`, `.limitations` | 제공됨. 원문 인용 대상은 아님 |
| 입력 | 인용 ID와 원문 | `data.evidence = merge_evidence(fixture, 상위 노드 evidence 전부)` | 제공됨. 상위가 검색했지만 인용하지 않은 근거도 포함 |
| 입력 | 미확인 사항 | `prior_results[role].unverified`; 상위 상태가 completed가 아니면 description에 “상위 노드 X: status” | 제공됨 |
| 입력 | 잠정 TRL | `prior_results["tech"].trl_estimates` (`provisional=true`) | 제공됨 |
| 입력 | 코드 규칙 gaps | `collect`가 계산한 `state["gaps"]`를 description에 JSON으로 추가 | 이번 변경으로 추가 |
| 출력 | SUMMARY의 입력·5장 시사점 본문 | `synthesis.result.summary` → 보고서 노드의 SUMMARY 작성 입력과 5.1장 | 최종 SUMMARY는 `report.result.summary`이며, 종합 요약은 5.1장에 표시 |
| 출력 | 관점별 일치·상충 판정 | `synthesis.result.assessments` (consistency/implications) → 5장 목록 | 사용됨 |
| 출력 | 최종 TRL | `trl_estimates`(`provisional=false`만) → `state["trl_result"]` → 3.3장, `validate_report`의 “TRL 미확인” 검사 | 사용됨. `provisional=true`면 조용히 버려지고 “확정 TRL 근거 부족”으로 대체되므로 이번 검사에서 오류로 노출 |
| 출력 | 실제 인용 출처 | `used_ids(out)` → `state["sources"]` → REFERENCE | 사용됨 |
| 출력 | 한계·미확인 | `limitations` → 6장, `unverified` → status와 gaps | 사용됨 |
| 출력 | 보고서 노드 입력 | `report`는 `prior_results`에 synthesis 결과를 포함해 받음 | 사용됨 |

## 2. 적용 기준과 문서 간 차이

| 항목 | 평가 가이드 | 설계서 | 적용 |
| --- | --- | --- | --- |
| 종합의 역할 | 관점 간 일치/불일치 의견 기반 종합, 상충 지점 명시, 중립 | C.3 TRL 확정, C.7 상충 보존, D.3 의견 맞추는 재조사 금지, E.2 5.1 교차표·5.2 보완 관계 | 모두 rubric·프롬프트에 반영. criterion ID는 계약 v2의 `consistency`, `implications` 유지 |
| TRL 어휘 | C.1 9단계 정의 | 표 7 KV cache 근거 유형(4 실험·코드, 5 프레임워크·실제 워크로드, 6 통합·시연, 7 파일럿, 8 출시, 9 운영) | 가이드 어휘를 기본으로, 표 7은 6~9단계 근거 점검표. 두 정의가 다르게 읽히면 낮은 단계 |
| TRL 확정 | 공개 정보 기반 추정 명시, 4~6 구간 공백 | 기술 조사 잠정 → 종합이 시장 상용화 근거와 대조해 확정 | 잠정 단계에서 시작, 시장 결과가 인용한 해당 기술 `scope=target` 근거가 있을 때만 상향. 근거 없으면 `level=null` |
| 애매한 경우 | 근거 없으면 확인 불가 | C.2 “애매하면 낮은 등급” | 적용하지 않음. 확인 불가/판정 유보 유지 ([최종 설계 검토](final-design-review.md)와 동일) |
| 종합 뒤 보완 | 언급 없음 | D.1·표 13 1라운드 | main의 `supplement` 노드가 gaps 담당 역할을 1회 재실행한 뒤 종합을 다시 호출(`limits.supplement=1`, mock 제외). 종합은 재실행 결과와 gaps를 데이터로 받아 한계에 남기며 보완 여부를 스스로 정하지 않음 |
| 추천 금지 | 우열 판정 아님 | E.1 추천 문서 아님 | rubric·프롬프트에 금지 명시. 평가기는 추천 표현을 검토 플래그로만 표시 |

## 3. 공통 검사가 보는 것과 보지 못하는 것

공통 Judge(`prompts/shared/judge.j2`)와 `contract_errors`는 claim의 인용 정합성, ID 존재, rubric 허용값, 질문별 assessment 존재, 확인 불가가 아닌 판정의 supported claim 연결만 확인합니다. 종합 노드에서 검사되지 않던 것은 다음과 같고, `runtime/synthesis_check.py`가 `node == "synthesis"`일 때만 추가로 검사합니다.

| 간극 | 검사 |
| --- | --- |
| 관점이 하나뿐인데 일치/상충을 판정 | 확인 불가가 아닌 상위 관점이 둘 미만이면 오류 |
| 한쪽 관점 근거만 인용한 일치/상충 | assessment.evidence_ids가 같은 기술의 확인 불가가 아닌 상위 assessment 두 관점의 인용 ID와 겹쳐야 함 |
| 관점 결과 없이 시사점 작성 | 확인 불가가 아닌 상위 관점이 없으면 조건부 시사점·판정 유보 불가 |
| 다른 기술 근거 전용 | KIVI claim/assessment가 ITME evidence를 인용하면 오류 (역도 동일). `other`·reference 문서는 허용하되 TRL에는 불가 |
| 최종 TRL의 근거 없는 상향 | 잠정 단계보다 높으면 시장 결과가 인용한 해당 기술 `scope=target` 근거를 포함해야 함 |
| TRL 플래그·범위 | `provisional=true`, 다른 기술·`scope=context` 근거, 잠정 TRL이 있는데 최종 항목이 없는 경우 오류 |
| 상위 미확인 삭제 | 상위 unverified의 모든 항목을 `"<role>: <원문>"` 그대로 보존해야 함 (항목 단위) |

여전히 검사하지 못하는 것: 일치/상충 판정 자체의 타당성, 상충 원인 서술의 정확성, summary 문장의 의미, 추천 뉘앙스. 이 부분은 `app.evaluate_synthesis`의 검토자 범위와 `manual_review` 항목으로 사람이 확인합니다.

공통 코드 변경과 영향:

- `graph/node_graph.py`: `contract_errors` 끝에 synthesis 전용 검사 호출 한 줄. 다른 노드의 검사·재시도·근거 ID·실패 전파는 그대로입니다.
- `runtime/models.py` `MockBackend`: synthesis일 때만 상위 unverified를 `"<role>: ..."`로 옮기고 잠정 TRL마다 `level=null, provisional=false`를 반환합니다. 다른 노드의 mock 출력은 변하지 않습니다.
- `graph/main_graph.py`: synthesis 입력 description에 `state["gaps"]`를 JSON으로 덧붙입니다. 프롬프트 해시가 synthesis에서만 바뀝니다.
- 알려진 공통 동작: `verify`는 `unverified`가 있으면 verdict를 “추가 근거 필요”로 두므로 종합 노드는 상위 미확인 항목을 보존하는 한 “통과”가 되지 않고, 계약 오류가 있어도 `fix` 단계가 실행되지 않습니다. 검색 예산이 없는 종합 노드는 그대로 `return_result`로 가서 판정이 확인 불가로 보류됩니다. 순서 변경은 서브그래프 담당과 별도 논의가 필요합니다.

## 4. 고정 입력과 검토자 사례

`tests/fixtures/synthesis/`의 `evidence.text`는 **원문 인용이 아니라 검토자 요약**입니다. 기술 담당의 `tests/fixtures/tech/trl_eval/`에서 원문 페이지를 확인해 만든 요약을 새 ID(`*-review-*`)로 재사용했고, 초록 요약 2건은 설계서 A.2 표 1과 대조해 추가했습니다. `prior_results`는 그 근거만으로 네 관점이 냈을 법한 결과를 검토자가 구성한 것이며 실제 노드 실행 결과가 아닙니다.

- `basic.json`: 초록 요약 2건과 기술 조사 결과만. 연결 확인용.
- `acceptance.json`: 두 기술 × 2항목. 시장·이해관계자는 웹 자료가 없어 전부 확인 불가, 도메인의 KIVI `quality=적합`은 초록 요약에만 근거해 기술 조사의 `limitations`(Falcon-7B 2비트 정확도 저하)와 상충하도록 구성.
- `missing-evidence.json`: 근거·상위 결과 없음. `needs_revision`이 정상.
- `eval/labels.json`: 검토자 판정 범위. `paper_only`는 KIVI 상충·ITME 일치, TRL은 잠정 단계(4, 5)를 넘지 못하고 한 단계 낮출 수 있음(3~4, 4~5). `no_prior`는 모든 항목 확인 불가와 `level=null`만 통과. `확인 불가`는 `inconclusive`로 보고합니다.

실제 Generator/Judge 실행은 사용자가 키를 설정하고 비용을 감수할 때만 합니다.

```bash
uv run python -m app.run_node --node synthesis --mode fixture --case acceptance
uv run python -m app.evaluate_synthesis --case paper_only --run outputs/local/<위 실행 디렉터리>
uv run python -m app.run_node --node synthesis --mode fixture --input tests/fixtures/synthesis/eval/no_prior.json
uv run python -m app.evaluate_synthesis --case no_prior --run outputs/local/<위 실행 디렉터리>
```

종료 코드는 `0=pass`, `2=fail/inconclusive`, `1=입력 오류`입니다. 채점기는 저장된 `input.json`이 사례와 같은지, `result.json`의 판정이 검토자 범위 안인지, 상충 양쪽 ID를 인용했는지, 상위 unverified를 항목 단위로 보존했는지 검사합니다. `review_flags`는 사람이 볼 힌트이며 판정을 바꾸지 않습니다.

## 5. 남은 간극과 하위 노드 영향

- 시장 직접 근거에 의한 TRL 상향은 검증된 웹 자료가 없어 회귀 사례로 만들지 못했습니다. 규칙은 단위 테스트로만 확인했습니다.
- 실제 LLM으로 acceptance를 실행한 결과는 아직 없습니다. 프롬프트 품질은 fixture 모드 실행 후 `evaluate_synthesis`와 `manual_review`로 확인해야 합니다.
- 상위 결과에 확인 불가가 하나라도 있으면 종합은 `needs_revision`이고, `validate_report`는 모든 노드 completed를 요구하므로 전체 보고서는 ready가 되지 않습니다. 이는 계약 v2의 정책이며 종합 노드에서 바꾸지 않았습니다.
- 보고서 노드는 `synthesis.summary`를 바탕으로 최종 SUMMARY를 쓰며, 5.1장에는 종합 요약이 표시됩니다. summary 작성 규칙(갈린 지점 우선, 확인 불가 개수, 추천 금지)을 프롬프트에 넣었지만 문장 검수는 사람이 해야 합니다.
- 시장 rubric의 출처 개수 조건과 도메인 rubric의 비용·도입 부적합 조건은 여전히 팀 해석 대상입니다. 종합은 그 조건으로 판정된 상위 결과를 조정하지 않고 limitations에 검토 대상으로 남깁니다.
