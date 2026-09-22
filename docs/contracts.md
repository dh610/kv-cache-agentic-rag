# 공통 입출력 계약 — v2

변경된 State와 criterion ID는 [정합성 이관 안내](design-alignment.md)를 참고하세요.

스키마의 원본은 `schemas/contracts.py`이며 모르는 필드는 거부합니다.
호출 방식·JSON 예시·필수 서지 필드·파일 책임·완료 기준은 [협업 계약](team-contract.md)에 명시했습니다.

| 형식 | 주요 필드 | 의미 |
| --- | --- | --- |
| NodeInput | case_id, target_techs, domain, questions, evidence, prior_results | 개별 노드 독립 실행에도 상위 결과 주입 가능 |
| Question | id, technology, criterion, text | 모든 질문을 실행하며 검색 예산은 질문별로 계산 |
| Evidence | id, text, title, url, technology, source_type, scope, document_role, page, citation_id, affiliation, affiliation_reason, stance | 변경 불가능한 인용 대상; 페이지는 1-based |
| Claim | id, technology, criterion, text, kind, evidence_ids, conditions | 사실/추론 분리 및 조건 보존 |
| Assessment | technology, criterion, judgment, rationale, evidence_ids | rubric에 허용된 판정만 사용 |
| NodeResult | node, summary, claims, assessments, unverified, limitations, trl_estimates | 모델의 공통 반환 형식 |
| NodeRun | status, result, evidence, checks, validation_errors, searches, prompt_hash, model, verdict, fix_count, coverage | 공통 런타임이 붙이는 검증/추적 결과 |

`NodeResult`는 모델이 생성하고, `NodeRun.status`는 코드가 결정합니다.
보고서 PR #10부터 report의 기존 `coverage` criterion은 `overview/market/stakeholder/domain/implications`로 나뉩니다. 보고서 개인 입력과 결과의 criterion도 함께 이관해야 하며, 허용 판정은 `구성 충족/부분 구성/확인 불가`입니다. 상세 절 대응은 [보고서 노드 문서](report-node.md)를 참고하세요. 다른 노드와 공통 JSON 외피는 유지합니다.
누락된 근거, Judge 미응답, 잘못된 ID, 미지원 주장을 모델의 자기 선언만으로 성공 처리하지 않습니다.
확인 불가가 아닌 판정은 같은 기술/기준의 supported claim을 근거로 가져야 합니다.
인정되는 근거는 해당 claim의 인용 ID와 Judge가 실제 확인한 인용 ID의 교집합입니다. claim이 여러 출처를 나열해도 Judge가 확인하지 않은 출처를 assessment에 사용할 수 없습니다. Judge가 claim의 모든 인용을 반복할 필요는 없지만, 최종 판정에 사용한 인용은 확인되어야 합니다.
기술 노드에서 `trl_estimates`를 제공하면 같은 기술의 `maturity.judgment`와 단계가 일치해야 합니다. `TRL 5`는 `level=5`, `확인 불가`는 `level=null`과 대응합니다. 기존 호출자의 빈 `trl_estimates`는 허용하며, 종합 노드가 추가 근거로 별도 판단한 단계까지 기술 노드와 강제로 일치시키지는 않습니다.
이 검사는 인용과 전제 연결을 확인하는 것이며 rationale의 모든 의미나 평가 등급의 타당성을 증명하지 않습니다.
summary도 LLM 요약이므로 최종 제출 전 원문과 검토해야 합니다.

- completed: 실행 계약과 인용 검사를 통과했고 미확인 항목이 없음. mock에서는 연결 확인만 의미합니다.
- needs_revision: 검색 실패/근거 부족/미확인/인용 검사 실패 등으로 검토 필요.
- failed: Generator/Judge 호출 또는 출력 파싱에 실패. 충분성 판정의 부분적 형식 실수(빠진/중복 질문, 모르는 evidence id, 다른 기술 근거 인용)는 해당 질문을 '부족'으로 정리해 계속 진행하고, 사유를 `coverage`에 남깁니다. 잘못된 인용 일부를 제거해도 '충분'으로 승격하지 않습니다. 유효한 질문 항목이 하나도 없는 응답은 failed를 유지합니다(설계서 D.3).

주장 검증 실패 시 해당 주장은 `unverified`에 남기고, 같은 기술·기준의 판정을 `확인 불가`로 보류합니다. 다른 항목의 검증된 주장·판정은 유지합니다. 실패 위치를 특정할 수 없는 계약 오류나 실행 실패는 전체 보류를 유지합니다.
검색 중 발생한 오류는 이후 재시도 성공과 별개로 이력에 남고 검토 상태를 유지합니다.
상위 노드 실패는 전체 상태에도 남습니다. 하위 노드를 계속 실행해 디버깅 결과는 얻지만 성공으로 승격하지 않습니다.

메인 State는 설계서의 입력·출처·TRL·gaps·파일 경로 키와 기존 `tech_result`, `market_result`, `stakeholder_result`, `domain_result`, `synthesis`, `report`를 함께 제공합니다. 역할별 결과 값은 공통 `NodeRun`으로 정규화했습니다. `report_path`는 PDF 경로이며 `report` 결과 객체와 별개입니다. 병렬 가지는 자기 결과 키만 쓰고, 세 가지가 끝난 뒤 한 번 합류합니다.
이전 결과를 원문 근거로 대신 사용하지 않습니다. 원문 evidence를 따로 병합하고 같은 ID의 다른 내용은 덮어쓰지 않습니다.

`app.check_handoff`는 rubric 전체 항목과 최종 인용의 서지 필드를 검사합니다.
확인 불가도 이유·미확인 목록·검색 이력이 충족되면 인수 가능한 응답일 수 있습니다.
이 검사는 NodeRun.status를 성공으로 바꾸지 않으며, 실제 기술 평가와 사람 검토의 완료를 대신하지 않습니다.


MainState: `target_techs`, `domain`, `limits`, `tech_result`, `market_result`, `stakeholder_result`, `domain_result`, `sources`, `trl_result`, `synthesis`, `gaps`, `supplement_round`, `report_path`; 호환/검증 확장 `report`, `report_check`, `run_status`.
`sources`는 누적 리듀서이며 사용 전 `merge_evidence`로 중복/충돌을 확인합니다. 같은 ID의 본문·URL·기술·문서·페이지·scope가 다르거나 affiliation/stance의 알려진 값끼리 상충하면 오류입니다. 제목·발행일·기관 같은 서지 메타데이터 차이는 먼저 본 항목을 유지하고 빈 필드만 채워 병합합니다(병렬 노드가 같은 웹 페이지를 각자 가져오면 Tavily 응답이 달라질 수 있음). unknown 관계를 채울 때는 그 판정의 사유도 함께 사용합니다. `gaps`는 코드 규칙으로 계산합니다.

종합 뒤 보완은 사용자 승인으로 기본 1라운드이며 `limits.supplement=0`이면 끕니다. 보완 이전 출처는 sources 이력에 남지만 최종 REFERENCE 및 서지 검사는 현재 노드 결과가 실제 인용한 근거에만 적용합니다. 더 이상 쓰지 않는 이전 근거의 서지 누락 때문에 보완된 최종 결과를 거부하지 않습니다.

### 질문별 재검색과 보완의 재사용

- 근거 충분성과 긍정·비판 검색 완료를 **질문별**로 확인합니다. 두 조건을 만족한 질문은 다른 질문의 부족 때문에 재검색하지 않습니다. 재작성 planner와 충분성 Judge도 해당 회차에서 처리할 질문만 받습니다.
- Generator·인용 Judge는 여전히 전체 질문을 작성·검증합니다. 특정 질문의 판정이 확인 불가이거나 그 질문의 claim이 unsupported이면 남은 예산 안에서 해당 질문만 다시 검색할 수 있습니다. 특정 질문으로 연결할 수 없는 미확인 문장은 그대로 보존하며 임의로 전체 질문을 재검색하거나 성공 처리하지 않습니다.
- 실패한 긍정/비판 검색은 수행 완료로 세지 않습니다. 남은 예산으로 누락된 intent를 다시 시도하고 실패 이력은 보존합니다.
- live 보완은 이전 `NodeRun`의 근거·coverage·검색 이력을 재사용합니다. 충분한 질문의 계획·검색·충분성 검사를 생략하고, 부족한 질문에만 새 검색 예산(최대 3회)을 적용합니다. `SearchRecord.attempt`는 이전 검색 뒤에서 누적 번호를 이어가므로 보완에서는 4 이상일 수 있습니다. 초기 실행과 보완 각 회차의 추가 횟수는 각각 최대 3회입니다.
- 기술 노드의 상태·미확인·기술별 주장/판정/TRL·인용 근거/한계가 실제로 바뀌면 의존 평가를 재실행합니다. 바뀐 기술의 기존 근거 충분성을 먼저 재검사하고 필요한 질문만 재검색합니다. 기술 결과가 그대로면 별도 gap이 없는 의존 노드를 다시 실행하지 않습니다.
- 재사용은 검색 결과에 한정합니다. 보완의 최종 평가 결과를 통째로 이전 값으로 복사하지 않으며, 전체 질문·인용·미확인 검증과 실패 전파를 유지합니다. 공개 `NodeInput`/`NodeRun` 필드는 변경하지 않았습니다.

RAGSubState의 설계 키: `role`, `questions`, `current_query`, `search_results`, `is_sufficient`, `draft`, `verdict`, `search_count`, `fix_count`, `output`.
목록 호출을 보존하므로 `search_count`는 질문 ID별 dict입니다. 내부 `queries`는 질문별 positive/critical 질의를 보관하고 `current_query`는 마지막 실행 질의입니다.

`ModelBackend`는 generate/judge 외 plan/sufficiency 메서드를 제공합니다. 실제 backend는 구조화된 QueryPlan/SufficiencyResult를 반환하고 mock은 오프라인 연결만 확인합니다.

PR #10의 서지 확장: Evidence에 선택적 `citation_id`와 `source_type=patent`를 추가했습니다. 논문·특허의 최종 서지 검사에서는 citation_id가 필요합니다. 기존 JSON은 파싱되지만 누락된 서지는 인수 검사에서 보완 대상으로 표시됩니다. 문서 메타데이터가 바뀌므로 각자 `uv run --extra rag python -m app.index`로 FAISS 인덱스를 재생성하세요.

### 입력 근거와 호출량 관리

- 최초 계획의 positive/critical 질의를 함께 실행한 뒤 충분성을 한 번 판단합니다. 제공자 호출은 노드당 최대 4개 동시 실행하며 추적 문맥을 전달합니다. 두 검색은 각각 질문별 예산을 사용하고, 실패 이력과 최대 3회 한도를 유지합니다. 부족할 때만 planner로 질의를 재작성합니다.
- 충분성 검사에는 해당 질문의 검색 이력·coverage에 연결된 근거를 전달합니다. 생성에는 충분성에서 인용한 근거와 각 검색의 출처 유형별 상위 2개 및 비판·상충으로 분류된 근거를 합치고, 부족한 질문의 검색 근거는 모두 유지합니다. 원문은 NodeRun.evidence에 계속 보관합니다.
- 긴 웹 본문은 생성/충분성 프롬프트에서 질의와 관련된 원문 구간과 한계 표현 구간을 발췌합니다. excerpt_ranges는 보관 원문의 문자 위치이며, 생략 부분의 사실이나 부재를 추정하면 안 됩니다. 논문 청크는 자르지 않습니다. 이 선택 방식의 실제 검색 품질은 별도 평가가 필요합니다.
- 인용 Judge에는 각 주장이 실제 인용한 원문을 **발췌 없이** 전달합니다. 생성 모델의 요약문을 인용 근거로 대체하지 않습니다.
- 상위 노드의 검색 자료 전체 대신 실제 결과가 사용한 근거만 다음 노드에 넘깁니다. 각 노드의 전체 검색 이력은 보존합니다.
- 실제 생성의 JSON 스키마에서 criterion별 judgment 허용값을 제한합니다. 생성 후 값을 임의 교체하지 않으며 기존 rubric/인용 검증을 유지합니다. 공개 NodeInput/NodeRun 및 담당자 프롬프트 변수는 그대로입니다.

### 1차 보고서 우선 모드

`./run-report.sh --first-pass` 또는 `python -m app.run_report --first-pass`는 실제 긍정·비판 검색(질문당 2회), 생성, 인용 검증, 종합, 보고서 생성을 수행합니다. 추가 재검색·전체 fix·종합 뒤 supplement는 이 실행에서만 0회이며 기본 설정은 그대로입니다. 보고서에 모드와 미확인을 명시하고, 검증 실패를 성공으로 처리하지 않습니다.

실제 검색 모드의 생성은 기술별 입력을 분리해 최대 2개 병렬 호출하고 결과를 합칩니다. 모든 질문을 유지하며 claim ID에 묶음 접두사를 붙여 충돌을 막습니다. 종합·보고서는 상위 근거를 사용하므로 새 검색어 계획·작성 전 충분성 LLM 호출을 생략하고 생성 후 인용·계약 검증은 계속 수행합니다. 실제 생성의 claim은 하나 이상의 근거 ID가 필수입니다.

1차 보고서 모드의 report 노드는 LLM으로 사실을 다시 쓰지 않고, 네 관점과 종합의 검증된 claim/판정/원문 ID를 보고서 절에 대응시킵니다. 원래 Judge 검증 결과를 ID에 맞춰 보존하고 공통 계약 검사를 다시 수행합니다. 근거 없는 절·상위 실패·미확인은 유지합니다. 기본 모드의 보고서 LLM 경로는 그대로입니다.
