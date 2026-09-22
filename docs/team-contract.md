# 노드 협업 계약 — 2026-09-22

이 문서는 최신 팀 대화에서 요청된 다섯 가지 연결 조건을 **현재 GitHub 구현의 계약**으로 구체화합니다.
모든 팀원이 역할 배정이나 최종 평가 등급까지 승인했다는 의미는 아닙니다.
원본 ZIP의 `agents/*.py`, `graph/rag_subgraph.py` 경로와 현재 저장소의 경로는 다릅니다.
현재 코드·이 문서·`schemas/contracts.py`를 같은 버전으로 사용하세요.

## 1. 호출과 질문 처리

호출자는 `NodeInput.questions` 목록을 한 번 전달하고, 공통 `graph/node_graph.py`가 **모든 질문을 순회**합니다.
질문마다 `id`, `technology`, `criterion`, `text`를 반드시 전달합니다.
두 기술의 같은 평가 기준도 서로 다른 질문 ID를 사용합니다.

- 예: `kivi-adoption`, `itme-adoption`은 각각 독립된 검색 예산을 가집니다.
- 검색 한도 3회는 첫 검색을 포함한 질문별 한도입니다. 생성/Judge는 현재 모은 근거로 항목 전체를 처리합니다.
- 재검색 라운드에서도 ID별 누적 횟수와 이전 근거를 보존합니다.
- mock/fixture는 고정 근거이므로 최초 1회만 조회합니다.
- 기본 최대 질문 수는 6개입니다. 시장 3항목 × 두 기술 = 6개가 한 번에 들어갑니다.

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
| 논문(`paper`) | `authors`, `year`, `venue`, `document_id`, `page`; `id`가 chunk_id 역할 |
| 웹(`web`) | `publisher` 또는 `authors`, `site`, `published_at`, `retrieved_at` |

논문의 page는 PDF 물리 페이지(1부터), URL은 원문 위치, venue는 게재 정보이며 프리프린트는 그렇게 명시합니다.
웹에서 발행일/기관을 못 찾으면 null로 남기고 보완 대상으로 기록합니다. 수집일로 발행일을 대체하거나 기관명을 추측하지 않습니다.
Tavily 어댑터는 반환된 author/publisher/date와 URL hostname을 기록하며 없는 정보를 만들지 않습니다.
REFERENCE에는 검색한 모든 자료가 아니라 실제 인용된 ID만 넘깁니다.

- `scope=target`: 선정 기술 직접 근거. `context`: 분야 배경/비교 자료.
- `document_role=target/reference`: 논문 corpus 내 문서의 역할. scope와 다른 축입니다.
- 분야 채택을 선정 기술 자체의 채택/TRL로 전용하지 않습니다.
- 웹 검색의 scope 기본값은 context입니다. 직접 근거라는 분류가 필요하면 원문을 사람이 확인한 입력에서 명시합니다.
- 같은 ID의 다른 내용은 오류로 처리합니다. 출처 병합은 `merge_evidence`를 사용하며 덮어쓰지 않습니다.

## 4. 수정 범위와 역할 분담

김계원님의 **평가 3명 + 검색 품질 1명 + 공통 기반 1명** 구조가 현재 플랫폼에 맞습니다.
현재는 공통 기반이 돌아가므로 세 평가 담당자가 `.j2`·rubric·fixture를 독립적으로 작업할 수 있습니다.
다음은 파일/산출물 기준 권장 분담이며, 개인 배정은 팀에서 확정합니다.

| 작업 묶음 | 담당 범위 | 책임 산출물 |
| --- | --- | --- |
| 시장 평가 1명 | `prompts/market`, `rubrics/market.yaml`, `tests/fixtures/market` | 두 기술 × 시장 3항목 결과 |
| 이해관계자 평가 1명 | `prompts/stakeholder`, 해당 rubric/fixture | 주체별 실제 반응·출처·조건 |
| 도메인 평가 1명 | `prompts/domain`, 해당 rubric/fixture | 클라우드 적용 조건·제약 비교 |
| 검색 품질 + 기술 사실 1명 | `rag/local_index.py`, `data/documents.yaml`, 기술 prompt/rubric, 향후 `data/eval` | 원문/표 점검, 검색 평가, 기술 조사·TRL 잠정 근거 |
| 공통 기반 + 종합/보고서 통합 1명 | `schemas`, `graph`, `runtime`, `rag/web.py`, 종합/보고서 prompt | 인터페이스, 웹/검증, 최종 인용·보고서 연결 |

검색 품질은 임베딩·청킹·정답 라벨·Hit/MRR를, 공통 기반은 호출·재시도·웹 어댑터·State·상태 전파를 책임집니다.
두 사람이 함께 `rag/` 전체를 수정하지 않도록 파일 경계를 나눕니다.
기술 조사 책임을 검색 품질 담당에, 종합/보고서 통합 책임을 기반 담당에 미리 둬 빈 역할을 막습니다.
종합/보고서는 먼저 결과 예시로 틀을 만들고, 먼저 끝난 팀원이 내용 검수에 합류하는 편이 안전합니다.

`schemas/`, `graph/`, `runtime/`, `prompts/shared/`, `config.yaml`, `pyproject.toml`, `uv.lock` 변경은 공통 영향 범위를 PR에 적어 함께 검토합니다.
의존성이 필요 없는 프롬프트 수정 PR에는 lock 파일을 바꾸지 않습니다.
팀원마다 개인 브랜치·.env·LangSmith 프로젝트·outputs 디렉토리를 사용합니다.

## 5. 완료 기준

시장 항목은 최종 설계 C.4의 범주에 맞춰 `growth`(시장 규모·성장성), `adoption`(상용화·채택), `ecosystem`(생태계 지지)로 정리했습니다.
두 기술 × 세 항목을 모두 반환하고, 각 항목에 이유 및 검증된 출처 또는 명시적 확인 불가가 있어야 합니다.
시장 judgment는 표 8의 `높음`, `보통`, `낮음`, `확인 불가`를 사용합니다.
현재 기준은 사용자 지정 설계서 `(2).pdf`의 C.2·C.4·표 8입니다. 세 항목의 등급 조건을 rubric에 반영했습니다.
근거가 있는 등급 판정이 애매하면 낮은 등급, 계획만 확인되면 한 등급 하향을 적용합니다.
논문·데모/원 저자 코드 외 적용·지원 미확인은 낮음, 평가 근거 자체가 없는 경우는 확인 불가로 구분합니다.
단일 도입 자동 보통 등 이전 별도 규칙은 제거했으며 변경 이력은 `final-design-review.md`에 남깁니다.
이해당사자 자료는 독립 출처와 함께 인용해도 claims.conditions에 출처 ID·기관·관계를 표시하고 limitations에 편향을 남깁니다.

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
두 기술의 균형과 상충 보존. 다른 역할의 최종 설계 항목 확대는 별도 작업이며 현재 rubric 범위 검사와 혼동하지 않습니다.
