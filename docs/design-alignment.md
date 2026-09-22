# 설계서 정합성 반영 및 팀 이관 — 계약 v2

2026-09-22 10:34 정재웅님의 피드백을 보완 항목으로 수용했습니다. 이 문서는 **구현된 기능**, **검증한 범위**, **남은 자료·의사결정**을 구분합니다. 코드 반영이 실제 기술 평가의 완료를 뜻하지 않습니다.

## 반영 범위

| 피드백 | 현재 구현 | 확인 방법 / 남은 일 |
| --- | --- | --- |
| Main State 공통 키 | `target_techs`, `domain`, `limits`, `sources`, `trl_result`, `gaps`, `supplement_round`, `report_path` 추가 | `graph/main_graph.py`, 전체 실행 `state.json` |
| sources 누적 | `Annotated[list[Evidence], operator.add]`로 각 노드의 사용 출처를 누적 | 사용 전 ID 중복 제거·동일 ID 내용 충돌 검사 유지. REFERENCE는 이 목록 사용 |
| gaps와 보고서 6장 | 확인 불가 비율, 핵심 항목 미확인, 검증 실패, 양쪽 검색 누락을 코드로 기록 | `gap_policy` 설정; 의견 차이만으로 gap을 만들지 않음 |
| TRL 최종 상태 | `NodeResult.trl_estimates` 구조와 근거 검사, 종합 결과를 `trl_result`에 연결 | 기술 담당의 잠정 판단 + 종합 담당의 실제 원문/시장 근거 검토 필요. 빈 결과는 단계 추측 없이 확인 불가 |
| report와 report_path | 기존 `report: NodeRun` 유지, 생성된 PDF 절대 경로를 `report_path`에 별도 기록 | 기존 호출자 호환; 문자열 경로와 평가 객체를 혼동하지 않음 |
| 입력 초기화 / finish | `initialize`에서 공통 값 초기화, 기존 finish 대신 `check_report`가 검사·상태 집계·파일 출력 | 보고서 뒤 검사 노드가 실제 그래프에 포함됨 |
| 공통 서브그래프 | `plan → search → check_sufficiency → write_draft → verify → return_result`, 조건부 `rewrite_query`, `fix`: 총 8개 | 충분성 부족/비판 검색 미수행 시 rewrite, 추가 근거 필요 시 남은 예산 안 재검색, 표현 오류는 1회 수정 후 재검증 |
| Sub State | 설계서 10개 이름 보존 + 검색 이력·검증 메타데이터 확장 | `RAGSubState`; 목록 입력을 지원하므로 `search_count`는 질문 ID별 dict. 단일 전역 횟수로 합치지 않음 |
| verdict | `통과 / 표현 오류 / 추가 근거 필요` | 주장별 label을 보존하고 코드가 노드 verdict를 집계; API/파싱 실패는 failed |
| 질의 이중언어화 | 실제 planner가 질문별 긍정/비판 질의를 작성하고 부족한 근거 피드백으로 재작성 | 기존 고정 영어 접미사 제거. mock의 계획은 오프라인 테스트용 |
| 검색 평가 사전 기준 | 30개·각 기술 15개·한국어·약어/수치 1/3·tie .05·Hit@5 .80/MRR .60 | `config.yaml.evaluation`, `app.evaluate_retrieval` |
| 미달 대응 | 청킹 실험 → 이중언어 → BGE-M3 learned sparse RRF → 리랭커 실행 경로 | `--run --remediate`; 모델·정답 원문 필요. 실측 미실행. 리랭커는 미달 대응이므로 기본 꺼짐 유지 |
| 등급·항목 | 시장 3항목 높음/보통/낮음/확인 불가, 이해관계자 3주체 긍정/중립/부정/확인 불가, 도메인 5항목 적합/조건부 적합/부적합/확인 불가 | rubric과 acceptance fixture 동시 반영; 기준 해석 쟁점은 아래 참고 |
| 이해당사자 출처 | `affiliation`, `affiliation_reason`, `stance` 추가 | 대상 원 논문은 first_party. ITME는 SK hynix 자사 연구로 표시. 웹은 `data/source_annotations.yaml`에서 사람이 검토한 URL만 분류 |
| 긍정·비판 검색 | 질문별 search intent와 원문 입장을 별도 기록 | 양쪽 검색 미완료는 gaps, 한쪽 자료/미분류는 limitations. 검색어가 비판이라고 검색 결과를 자동으로 비판 자료로 단정하지 않음 |
| 공개 정보 추정 | 공통 프롬프트와 TRL 구조에 명시 | 사실/추론 분리와 근거 검사 유지 |
| 종합의 상충 보존 | rubric에 상충 보존·의견을 맞추는 재조사 금지 명시, 새 검색 미사용 | 기존 조건부 권고를 조건부 시사점으로 변경; 특정 기술 추천 금지 |
| 보고서 | SUMMARY–REFERENCE Markdown/PDF, 관점별 기술 비교표, TRL·gaps·사용 출처 | `report_check.ready`가 false면 실제 모드에서 정상 완료하지 않음. mock의 completed는 연결 확인일 뿐 |

## 의도적으로 남긴 범위 결정

- **종합 뒤 자동 보완:** 설계서 표 13대로 1라운드를 `graph/main_graph.py`의 `supplement` 노드로 구현(`limits.supplement`, 0이면 끔). gaps 의 담당 역할만 재실행하고, 기술 조사가 바뀌면 의존 평가도 재실행한 뒤 synthesis 로 복귀. mock 모드는 고정 문자열 연결 점검이라 건너뛴다.
- **rubric 조건 충돌:** 등급 어휘와 항목은 맞췄으나, 출처 수만으로 성장성을 판정하거나 비용 정보가 없다는 이유만으로 부적합으로 내리는 조건은 원문/조건 충돌을 명시하고 확인 불가로 보류합니다. 팀의 조건 해석 합의가 필요합니다. `docs/final-design-review.md`의 해석 쟁점을 그대로 숨기지 않습니다.
- **실측·자료:** 30문항의 사람 검증, 3모델 실측, reference 논문 2편 등록, 실제 웹 출처 분류/발행일 확인, 역할별 실제 LLM 품질 검토는 남아 있습니다. ZIP의 성능 수치를 현재 코드의 실측값으로 복사하지 않았습니다.
- **출력 검수:** PDF 생성과 자동 검사는 구현했지만 실제 기술 결론, 모든 요약 문장, 평가 등급 타당성의 사람 승인을 대체하지 않습니다. 도메인·종합·보고서 내용 책임자는 여전히 미정입니다.

## 팀원이 맞춰야 할 인터페이스

- `config.yaml.schema_version=2`; `uv sync --frozen` 재실행. 실제 검색/평가는 `uv sync --frozen --extra rag`.
- 공개 입력 `NodeInput.questions` 목록과 반환 `NodeRun`은 유지. 내부 State 이름/단계가 바뀌었으므로 기존 graph 코드를 통째로 덮어쓰지 않습니다.
- `ModelBackend`를 직접 구현한 경우 `plan(data, feedback) -> QueryPlan`, `sufficiency(data, evidence) -> SufficiencyResult`도 구현합니다. 출력은 런타임에서 다시 검증합니다.
- `EvidenceSource.search(Question, attempt)`는 유지. Question.text에 실제 재작성된 질의가 전달됩니다. 어댑터에서 예전 고정 접미사를 또 붙이지 않습니다.
- 이해관계자 criterion ID: `competitors`, `adopters`, `industry`. 도메인: `cost`, `performance`, `quality`, `operations`, `scalability`. 종합: `consistency`, `implications`. 이전 `reaction/conflict`, `fit`, `recommendation` 입력은 새 fixture를 참고해 바꿉니다.
- 기술의 `mechanism/maturity/limitations` ID는 유지. maturity는 TRL 1~9/확인 불가를 허용. 김계원님의 PR #4 프롬프트·TRL 평가 사례는 최신 main에서 받아 보존했습니다. TRL 세부 단계는 기술 프롬프트의 실습 가이드 정의를 유지합니다.
- 질문 한도 기본값은 10개: 두 기술 × 도메인 5항목을 한 번에 처리합니다. 검색 예산은 여전히 질문별 최초 포함 3회이고 수정은 노드당 최대 1회입니다.
- `sources`는 State에서 누적될 수 있습니다. 소비자는 `merge_evidence`로 고유 ID를 만들고 사용 출처와 인용을 대조합니다.
- 이전 JSON/캐시/모델 결과를 새 계약에 맞춘 검증 결과로 재사용하지 않습니다. 문서 메타데이터가 바뀌었으므로 기존 FAISS 인덱스는 재생성합니다.

## 각자 브랜치에 최신 main 반영

진행 중인 변경은 먼저 본인 브랜치에 커밋하거나 안전하게 보관합니다. 미커밋 작업을 지우지 않습니다.

```bash
git status --short --branch
# 본인 작업 브랜치에서 변경을 정리한 뒤:
git fetch origin
git merge origin/main
uv sync --frozen
# 본인 노드로 바꿔서 실행
uv run python -m app.run_node --node market --mode mock --case acceptance
uv run pytest -q
```

main에서 새 작업을 시작하는 경우에만 `git switch main`, `git pull --ff-only`, 새 개인 브랜치 생성 순서를 사용합니다.
기존 작업 브랜치에서 단순 `git pull`만 하면 그 브랜치의 원격만 갱신될 수 있어 main 변경이 들어오지 않습니다.
공통 코드/테스트 충돌은 이번 8단계·State 계약을 유지하면서 담당 기능을 이식하고, 파일 전체를 ours/theirs로 덮어쓰지 않습니다.
