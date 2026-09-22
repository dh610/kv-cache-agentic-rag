# 노드 협업 계약 — 2026-09-22

이 문서는 최신 팀 대화에서 요청된 다섯 가지 연결 조건을 **현재 GitHub 구현의 계약**으로 구체화합니다.
모든 팀원이 역할 배정이나 최종 평가 등급까지 승인했다는 의미는 아닙니다.
계약 v2의 State/단계 변경과 이관 절차는 [정합성 반영 문서](design-alignment.md)에 있습니다.
원본 ZIP의 `agents/*.py`, `graph/rag_subgraph.py` 경로와 현재 저장소의 경로는 다릅니다.
현재 코드·이 문서·`schemas/contracts.py`를 같은 버전으로 사용하세요.

## 1. 호출과 질문 처리

호출자는 `NodeInput.questions` 목록을 한 번 전달하고, 공통 `graph/node_graph.py`가 **모든 질문을 순회**합니다.
질문마다 `id`, `technology`, `criterion`, `text`를 반드시 전달합니다.
두 기술의 같은 평가 기준도 서로 다른 질문 ID를 사용합니다.

- 예: `kivi-adoption`, `itme-adoption`은 각각 독립된 검색 예산을 가집니다.
- 검색 한도 3회는 첫 검색을 포함한 질문별 한도입니다. 작성 전 충분성 Judge가 현재 모은 근거를 질문별로 평가하고, 생성/인용 Judge는 항목 전체를 처리합니다.
- 재검색 라운드에서도 ID별 누적 횟수와 이전 근거를 보존합니다.
- mock/fixture는 고정 근거이므로 최초 1회만 조회합니다.
- 기본 최대 질문 수는 10개입니다. 시장 3항목 × 두 기술 = 6개, 도메인 5항목 × 두 기술 = 10개가 한 번에 들어갑니다.

PDF D.2의 ‘호출자가 하나씩 호출’과 공개 API의 모양은 다릅니다. 질문별 예산과 기술 구분은 동일한 의도로 구현했습니다.
기존 `run_rag_subgraph(role, questions, limits)`를 별도로 다시 만들지 말고 현재 실행기를 사용합니다.

```bash
uv run python -m app.run_node --node market --mode mock --case acceptance
uv run python -m app.run_node --node market --mode fixture --input data/private/market-case.json
```

첫 명령은 연결 점검, 두 번째는 실제 Generator/Judge 실행입니다. 실제 근거를 넣어야 실제 평가를 할 수 있습니다.
Python에서 직접 연결할 경우 공통 진입점은 다음과 같습니다.

```python
from graph.node_graph import build_node_graph

# data: NodeInput, settings: Settings, backend: ModelBackend, source: EvidenceSource
run = build_node_graph("market", data, "fixture", settings, backend, source).invoke({})["output"]
# run: NodeRun. 전체 State에서는 state["market_result"]에 저장.
```

## 2. 결과 JSON

반환은 모든 역할에서 `NodeRun` 하나입니다. 역할마다 별도의 `items`/`output`/`sources` 외피를 만들지 않습니다.

| 팀 대화의 개념 | 실제 필드 |
| --- | --- |
| 항목 목록(items) | `result.assessments` |
| 기술(tech) | `assessment.technology` |
| 항목(item) | `assessment.criterion` — rubric ID |
| 등급(grade) | `assessment.judgment` — rubric의 허용값 |
| 이유(reason) | `assessment.rationale` |
| 인용 출처(sources) | `assessment.evidence_ids` → `NodeRun.evidence[].id` |
| 미확인 | `result.unverified` — 기술/기준 ID와 함께 기록 |
| 검증 가능한 주장 | `result.claims` + `NodeRun.checks` |
| 실행 상태 | `NodeRun.status`, `validation_errors`, `searches` |

시장 6항목의 [결과 형식 예시](examples/market-result.json)를 제공합니다. 내용은 형식 설명용이며 실제 기술 평가가 아닙니다.
이 파일은 `NodeResult` 부분의 예시이며 전체 `NodeRun`에는 상태·근거·검증·실행 이력이 추가됩니다.
종합/보고서 담당자는 `state["market_result"].result.assessments`를 읽고 인용 ID를 `evidence`에 연결합니다.

## 3. 출처 형식과 인수 검사

아래 필드는 **최종 인수 때 실제 인용한 출처**의 필수 항목입니다.
검색 중 누락될 수 있는 서지 필드는 Pydantic에서 null을 허용하지만, 인수 검사에서 누락을 보고합니다.

| 출처 | 필수 항목 |
| --- | --- |
| 공통 | `id`, `text`, `title`, `url`, `technology`, `source_type`, `scope`, `document_role` |
| 논문(`paper`) | `authors`, `year`, `venue`, `citation_id`(권(호)·페이지 또는 arXiv 번호), `document_id`, `page`; `id`가 chunk_id 역할 |
| 웹(`web`) | `publisher` 또는 `authors`, `site`, `published_at`, `retrieved_at` |
| 특허(`patent`) | `publisher`(출원인), `published_at`(YYYY-MM), `citation_id`(특허번호/공개번호) |

논문의 page는 PDF 물리 페이지(1부터), URL은 원문 위치, venue는 게재 정보이며 프리프린트는 그렇게 명시합니다.
웹에서 발행일/기관을 못 찾으면 null로 남기고 보완 대상으로 기록합니다. 수집일로 발행일을 대체하거나 기관명을 추측하지 않습니다.
Tavily 어댑터는 반환된 author/publisher/date와 URL hostname을 기록하며 없는 정보를 만들지 않습니다.
`affiliation`(자사/독립/미확인), `affiliation_reason`, `stance`를 보존합니다. 웹 원문을 사람이 확인한 관계·발행일은 기술별 URL을 키로 `data/source_annotations.yaml`에 기록합니다.
양쪽 검색 수행 여부는 SearchRecord.intent로, 실제 자료의 입장은 Evidence.stance로 구분합니다.
REFERENCE에는 검색한 모든 자료가 아니라 실제 인용된 ID만 넘깁니다.

- `scope=target`: 선정 기술 직접 근거. `context`: 분야 배경/비교 자료.
- `document_role=target/reference`: 논문 corpus 내 문서의 역할. scope와 다른 축입니다.
- 분야 채택을 선정 기술 자체의 채택/TRL로 전용하지 않습니다.
- PR #23부터 웹 검색 scope는 검색 층(target/context)을 기록합니다. 이 값만으로 실제 직접 채택·관계가 증명되지는 않으므로 원문과 source_annotations를 함께 확인합니다.
- 같은 ID의 다른 내용은 오류로 처리합니다. 출처 병합은 `merge_evidence`를 사용하며 덮어쓰지 않습니다.

## 4. 수정 범위와 역할 분담

역할 배정은 2026-09-22 09:54 김계원님의 팀 메시지를 반영했습니다.
아래 파일 경계는 그 배정을 현재 저장소 구조에 대응한 작업 기준입니다. 평가 담당자는 `.j2`·rubric·fixture를 독립적으로 개발합니다.

| 담당자 (GitHub) | 배정 역할 | 주 수정 범위 | 책임 산출물 |
| --- | --- | --- | --- |
| 김계원 (`wonn2k`) | 기술 | `prompts/tech/`, `rubrics/tech.yaml`, `tests/fixtures/tech/` | 두 기술의 원리·성숙도·실험 조건 평가 |
| 인수연 (`1nyeonart`) | 시장성 | `prompts/market/`, `rubrics/market.yaml`, `tests/fixtures/market/` | 두 기술 × 시장 3항목 결과 |
| 박유진 (`youjin09222`) | 이해관계자 | `prompts/stakeholder/`, `rubrics/stakeholder.yaml`, `tests/fixtures/stakeholder/` | 주체별 반응·출처·조건 평가 |
| 윤동현 (`dh610`) | RAG 및 성능 테스트 | `rag/local_index.py`, `app/index.py`, `data/documents.yaml`, `tests/test_index.py`, 향후 `data/eval/` | 임베딩·청킹·논문 인덱싱, 정답 근거와 검색 품질 측정 |
| 정재웅 (`Jae-Ung-Jeong`) | 검색 및 서브그래프 | `rag/web.py`, `graph/node_graph.py`, `graph/main_graph.py`, 관련 검색/그래프 테스트 | 웹 검색·재검색·질문 순회·검증·그래프 연결 |

윤동현은 로컬 논문 검색, 정재웅은 웹 검색과 그래프 실행 흐름을 우선 담당합니다.
`rag/` 전체를 양쪽이 동시에 수정하지 않습니다. 기술 평가 프롬프트의 담당자는 김계원이며 RAG 담당에 포함하지 않습니다.
`data/eval/`은 앞으로 만들 평가 자료 경로이며 아직 완성된 평가셋은 없습니다.

**미정 역할:** `domain`(도메인 평가), `synthesis`(종합), `report`(보고서)의 최종 내용 책임자는 위 메시지에 없습니다.
해당 `prompts/<node>/`, `rubrics/<node>.yaml`, `tests/fixtures/<node>/`는 기반만 있는 상태입니다.
팀에서 배정하거나 사용자가 명시적으로 작업을 맡기기 전에는 다른 역할에 자동 편입하지 않습니다. 기존 그래프에서 이 노드들을 삭제한다는 뜻은 아닙니다.

**공동 검토 파일:** `schemas/`, `runtime/`, `rag/interface.py`, `rag/evidence.py`, `prompts/shared/`, `config.yaml`, `pyproject.toml`, `uv.lock`, 공통 실행기와 테스트.
이 파일은 단독 소유 범위가 아니며, 영향받는 담당자와 변경 내용을 공유하고 PR에 입출력·호출 측 영향을 적습니다.
정재웅의 그래프 수정도 여러 노드에 영향을 주므로 동일한 검토 원칙을 적용합니다.
평가 담당자가 검색 기능 변경을 필요로 하면 자기 프롬프트 PR에 공통 검색 수정을 섞기보다 관련 담당자와 별도 PR로 연결합니다.
의존성이 필요 없는 프롬프트 수정 PR에는 lock 파일을 바꾸지 않습니다.
팀원마다 개인 브랜치·.env·LangSmith 프로젝트·outputs 디렉토리를 사용합니다.
브랜치 생성·검증·PR 절차는 [팀원 시작 안내](onboarding.md#git-작업-절차)를 따릅니다. main에 직접 수정·커밋·push하지 않습니다.

## 5. 완료 기준

시장 항목은 최종 설계 C.4의 범주에 맞춰 `growth`(시장 규모·성장성), `adoption`(상용화·채택), `ecosystem`(생태계 지지)로 정리했습니다.
두 기술 × 세 항목을 모두 반환하고, 각 항목에 이유 및 검증된 출처 또는 명시적 확인 불가가 있어야 합니다.
시장 judgment는 표 8의 `높음`, `보통`, `낮음`, `확인 불가`를 사용합니다.
현재 기준은 사용자 지정 설계서 `(2).pdf`의 C.2·C.4·표 8입니다. 세 항목의 등급 조건을 rubric에 반영했습니다.
근거가 있는 등급 판정이 애매하면 낮은 등급, 계획만 확인되면 한 등급 하향을 적용합니다.
논문·데모/원 저자 코드 외 적용·지원 미확인은 낮음, 평가 근거 자체가 없는 경우는 확인 불가로 구분합니다.
단일 도입 자동 보통 등 이전 별도 규칙은 제거했으며 변경 이력은 `final-design-review.md`에 남깁니다.
이해당사자 자료는 독립 출처와 함께 인용해도 claims.conditions에 출처 ID·기관·관계를 표시하고 limitations에 편향을 남깁니다.
계약 v2의 affiliation/affiliation_reason/stance를 보존하고 검색 intent와 자료 입장을 구분합니다.
시장 노드의 trl_estimates는 빈 목록이며, 시장 전용 검증기는 verdict·coverage·fix_count를 함께 확인합니다.
이 시장 브랜치는 제출 설계서 준수 요청에 따라 출처 수와 축소 전망의 충돌을 C.2의 낮은 등급으로 처리합니다.
main 이관 당시의 확인 불가 보류 해석과 차이가 있으므로 시장 PR의 팀 검토 사항으로 남기며, 다른 역할의 기준은 바꾸지 않습니다.

```bash
# 담당 노드의 모든 현재 rubric 항목을 요청하는 입력으로 실행
uv run python -m app.run_node --node market --mode fixture --case acceptance
# 위 명령의 Results 디렉토리로 경로를 바꿔 실행
uv run python -m app.check_handoff --node market --result outputs/local/RUN/result.json
# 사용자 입력으로 실행했다면 동일한 입력도 명시
uv run python -m app.check_handoff --node market --input data/private/market-case.json --result outputs/local/RUN/result.json
```

`check_handoff`는 항목 전체 포함, 이유 누락, mock/더미, 인용/Judge 실패, 검색 오류, 사용한 출처의 서지 누락을 검사합니다.
종료 코드 0은 **인수 형식 충족**, 2는 보완 필요, 1은 파일/설정 오류입니다.
근거를 못 찾았지만 모든 항목을 정직하게 확인 불가로 반환했다면 인수 형식은 충족할 수 있습니다.
그때도 `NodeRun.status=needs_revision`과 미확인 목록은 유지됩니다. 인수 통과가 기술 결론의 완성이나 정확성을 뜻하지 않습니다.
기본 acceptance의 발췌는 데모 자료이므로 그대로 최종 근거로 인수할 수 없습니다. 실제 근거로 교체하거나 한계를 명시해야 합니다.

사람이 확인할 항목: 실제 Generator/Judge 호출, 숫자·조건과 원문 일치, 등급 정의와 판정 일치, 자사/독립 출처 구분,
두 기술의 균형과 상충 보존. 이해관계자 3주체·도메인 5항목도 acceptance에 반영했습니다. 항목 존재 검사는 실제 평가 품질을 증명하지 않습니다.
