# 공통 입출력 계약 — v2

변경된 State와 criterion ID는 [정합성 이관 안내](design-alignment.md)를 참고하세요.

스키마의 원본은 `schemas/contracts.py`이며 모르는 필드는 거부합니다.
호출 방식·JSON 예시·필수 서지 필드·파일 책임·완료 기준은 [협업 계약](team-contract.md)에 명시했습니다.

| 형식 | 주요 필드 | 의미 |
| --- | --- | --- |
| NodeInput | case_id, target_techs, domain, questions, evidence, prior_results | 개별 노드 독립 실행에도 상위 결과 주입 가능 |
| Question | id, technology, criterion, text | 모든 질문을 실행하며 검색 예산은 질문별로 계산 |
| Evidence | id, text, title, url, technology, source_type, scope, document_role, page, affiliation, affiliation_reason, stance | 변경 불가능한 인용 대상; 페이지는 1-based |
| Claim | id, technology, criterion, text, kind, evidence_ids, conditions | 사실/추론 분리 및 조건 보존 |
| Assessment | technology, criterion, judgment, rationale, evidence_ids | rubric에 허용된 판정만 사용 |
| NodeResult | node, summary, claims, assessments, unverified, limitations, trl_estimates | 모델의 공통 반환 형식 |
| NodeRun | status, result, evidence, checks, validation_errors, searches, prompt_hash, model, verdict, fix_count, coverage | 공통 런타임이 붙이는 검증/추적 결과 |

`NodeResult`는 모델이 생성하고, `NodeRun.status`는 코드가 결정합니다.
누락된 근거, Judge 미응답, 잘못된 ID, 미지원 주장을 모델의 자기 선언만으로 성공 처리하지 않습니다.
확인 불가가 아닌 판정은 같은 기술/기준의 supported claim을 근거로 가져야 합니다.
인정되는 근거는 해당 claim의 인용 ID와 Judge가 실제 확인한 인용 ID의 교집합입니다. claim이 여러 출처를 나열해도 Judge가 확인하지 않은 출처를 assessment에 사용할 수 없습니다. Judge가 claim의 모든 인용을 반복할 필요는 없지만, 최종 판정에 사용한 인용은 확인되어야 합니다.
기술 노드에서 `trl_estimates`를 제공하면 같은 기술의 `maturity.judgment`와 단계가 일치해야 합니다. `TRL 5`는 `level=5`, `확인 불가`는 `level=null`과 대응합니다. 기존 호출자의 빈 `trl_estimates`는 허용하며, 종합 노드가 추가 근거로 별도 판단한 단계까지 기술 노드와 강제로 일치시키지는 않습니다.
이 검사는 인용과 전제 연결을 확인하는 것이며 rationale의 모든 의미나 평가 등급의 타당성을 증명하지 않습니다.
summary도 LLM 요약이므로 최종 제출 전 원문과 검토해야 합니다.

- completed: 실행 계약과 인용 검사를 통과했고 미확인 항목이 없음. mock에서는 연결 확인만 의미합니다.
- needs_revision: 검색 실패/근거 부족/미확인/인용 검사 실패 등으로 검토 필요.
- failed: Generator/Judge 호출 또는 출력 파싱에 실패. 충분성 판정기의 형식 실수(빠진 질문, 모르는 evidence id, 다른 기술 근거 인용)는 실패가 아니라 해당 질문을 '부족'으로 정리해 계속 진행하고, 사유를 `coverage`에 남긴다(설계서 D.3).

주장 검증 실패 시 해당 주장은 `unverified`에 남기고, 노드 판정은 보수적으로 `확인 불가`로 보류합니다.
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

RAGSubState의 설계 키: `role`, `questions`, `current_query`, `search_results`, `is_sufficient`, `draft`, `verdict`, `search_count`, `fix_count`, `output`.
목록 호출을 보존하므로 `search_count`는 질문 ID별 dict입니다. 내부 `queries`는 질문별 positive/critical 질의를 보관하고 `current_query`는 마지막 실행 질의입니다.

`ModelBackend`는 generate/judge 외 plan/sufficiency 메서드를 제공합니다. 실제 backend는 구조화된 QueryPlan/SufficiencyResult를 반환하고 mock은 오프라인 연결만 확인합니다.
