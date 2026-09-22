# 도메인 평가 노드 — 기준·검증·평가 사례

`domain` 노드의 내용 책임자는 [협업 계약](team-contract.md#4-수정-범위와-역할-분담)상 미정입니다. 이 문서는 사용자가 명시적으로 맡긴 작업 브랜치 `agent/domainv2`에서 정리한 현재 구현 상태이며, 역할 배정을 확정하지 않습니다. 2026-09-22에 개인 키로 fixture 모드 실제 Generator/Judge 실행을 수행했고 결과는 6절에 있습니다. 원문 PDF 대조는 하지 않았습니다.

## 1. 상위·하위 노드와 주고받는 정보

| 방향 | 실제 필드 | 내용 | 현재 상태 |
| --- | --- | --- | --- |
| 입력 ← 호출자 | `NodeInput.questions` | 두 기술 × 5항목 = 10개 질문(한도 10). ID는 `kivi-cost`처럼 기술·항목별로 분리 | acceptance fixture가 구체 질문으로 요청 |
| 입력 ← tech | `prior_results["tech"]` (`NodeResult`) | 기술별 원리·실험 조건·한계 claims, TRL 잠정 추정, unverified | 참고용. 프롬프트가 원문 증거로 쓰지 않도록 지시 |
| 입력 ← tech | `NodeInput.evidence` (main graph가 `merge_evidence`로 합침) | tech가 인용한 원문 evidence | 도메인 판정의 직접 근거로 인용 가능 (같은 기술·scope=target일 때) |
| 입력 ← 검색 | `search_results` | live: target 논문 RAG + 웹. mock/fixture: 고정 근거 | 검색 품질은 다른 담당자 범위 |
| 출력 → synthesis/report | `result.assessments` | (기술, 항목, 판정, rationale, evidence_ids) 10개 | 계약 검사가 항목 누락·허용 판정·근거 연결을 확인 |
| 출력 → synthesis/report | `result.claims` | 사실/추론 분리, `conditions`에 모델·하드웨어·워크로드·기준선·출처 범위 | 도메인 규칙이 실험 항목 claim의 conditions 누락을 검사 |
| 출력 → synthesis/report | `result.unverified`, `limitations` | 확인 불가 항목과 필요한 자료, 자사 편중·상충·설계서 해석 검토 대상 | 프롬프트가 형식을 지정 |
| 출력 → collect | `gaps` (코드 규칙) | `quality`, `cost`가 확인 불가면 핵심 항목 gap | `config.yaml.gap_policy` |
| 출력 → REFERENCE | `evidence` 중 실제 인용 ID | 서지 필드 누락은 `check_handoff`가 보고 | fixture 근거는 최종 출처가 될 수 없음 |

## 2. 평가 기준: 가이드·설계서·저장소 적용 기준

가이드 C.4는 도메인을 "데이터센터·클라우드: 대규모, 비용 민감"으로만 정의하고 항목·등급은 조에 맡깁니다. 설계서 표 10이 5항목 × 4등급을 정의하며, 저장소 `rubrics/domain.yaml`은 그 표를 따르되 [최종 설계 검토](final-design-review.md)에서 지적된 해석 쟁점을 다음처럼 적용합니다.

| 항목 | 설계서 표 10 | 저장소 적용 기준 | 차이 이유 |
| --- | --- | --- | --- |
| cost 부적합 | 신규 인프라 투자 + 절감 근거 없음 | 비용 요구조건 미충족의 **사실 근거**가 있을 때만. 신규 인프라만 확인되면 **확인 불가** + limitations에 해석 검토 대상 기록 | 미확인 비용과 요구조건 미충족을 분리 |
| performance 적합 | 논문 실험과 외부 사례 모두 | 동일. 논문 근거만 있으면 최대 조건부 적합. 서로 다른 실험의 수치를 우열로 해석하지 않음 | 가이드 "우열 판정 아님" |
| quality 적합 | 손실 없음/무시할 수준 | 동일하되 **원문 품질 보고가 있을 때만**. 데이터를 바꾸지 않는 방식이라는 추론만으로 적합 불가 | 미확인을 긍정으로 바꾸지 않음 |
| operations 부적합 | 신규 장비 + SW 스택 변경 | 양립 불가의 사실 근거가 있을 때만. 장비+SW 변경만 있으면 **조건부 적합** + 해석 검토 대상 기록 | 통합 부담과 도메인 부적합을 분리 |
| scalability | 규모 증가 시 효과 감소 근거면 부적합 | 동일. 모든 rationale에 논문 규모와 데이터센터 규모 차이 기록 | 설계서 C.6 요구 |
| 공통 (C.2) | 애매하면 낮은 등급 | **적용하지 않음**. 애매·부족은 확인 불가 | AGENTS.md와 계약 v2 |

등급 어휘(적합/조건부 적합/부적합/확인 불가)와 criterion ID(`cost`, `performance`, `quality`, `operations`, `scalability`)는 계약 v2와 같으며 `config.yaml.gap_policy`, 보고서 라벨과 연결돼 있습니다.

## 3. 검증 범위

| 검사 | 위치 | 확인하는 것 | 확인하지 못하는 것 |
| --- | --- | --- | --- |
| 인용 Judge | `prompts/shared/judge.j2` | claim과 인용 원문의 정합성(supported/misstated/unsupported) | 판정 등급의 타당성, 필수 조건 충족 |
| 계약 검사 | `graph/node_graph.py:contract_errors` | 항목 누락, 허용 판정, 근거 ID 존재, 확인 불가 아닌 판정의 supported claim 연결 | 인용 근거가 **같은 기술의 직접 근거**인지, 등급별 필수 조건 |
| 도메인 draft 규칙 | `runtime/domain_checks.py:draft_rule_errors` — `runtime/node_rules.py:DRAFT_RULES`로 등록, 공통 그래프의 `verify`가 Judge 호출 전에 실행 | 근거 부재를 말하는 claim(“명시되지 않”, “미상” 등) 금지, 다른 기술 근거 인용 금지, 확인 불가 아닌 판정의 직접 근거(같은 기술·scope=target) 필수, performance 적합의 외부(web) 사례 필수, 실험 항목 fact claim의 conditions 필수, 판정 항목 rationale의 "공개 정보 기반 추정" | rationale 문장의 의미, 수치·기준선이 원문과 일치하는지, FPGA 시제품과 CMM 플랫폼 측정을 섞었는지 |
| 도메인 handoff 규칙 | `runtime/domain_checks.py:domain_rule_errors` — `HANDOFF_RULES`로 `check_handoff`에 등록, 채점기가 재사용 | draft 규칙 + 부적합의 supported fact claim 필수 | 위와 같음 |
| 평가 채점기 | `app/evaluate_domain_fit.py` | 검토자 허용 범위(accepted/disputed)와 필수 근거 ID, supported claim 연결. `--draft`로 검증 전 초안(`draft.json`)도 채점 | 위와 같음. `manual_review` 항목은 사람이 확인 |

draft 규칙 위반은 그래프에서 `표현 오류`로 처리되어 기존 1회 수정 루프가 재작성 피드백을 받습니다. 수정 후에도 남으면 validation_errors에 기록되고 기존대로 판정이 보류됩니다. 이를 위해 공통 `graph/node_graph.py`에 세 가지를 추가했습니다(검색·서브그래프 담당자 검토 필요): 근거가 0건이면 충분성 Judge를 호출하지 않고 전 질문 불충분으로 기록, claim이 0건이면 인용 Judge를 호출하지 않음, 수정 예산이 남아 있는 동안 표현 오류를 `추가 근거 필요`보다 먼저 처리. 질문 순회·검색 예산·근거 ID·실패 전파는 그대로입니다. `app/run_node.py`는 검증 전 초안을 `draft.json`으로 함께 저장합니다(프롬프트 검토용이며 인수 산출물이 아님).

## 4. 평가 사례 (`tests/fixtures/domain/fit_eval/`)

`evidence.text`는 **원문 발췌가 아니라 검토자 요약**이며 `source_type=fixture`입니다. 원문 PDF는 저장소에 없으므로 기술 노드의 TRL 사례가 이미 검토한 페이지(KIVI 2·6·8쪽, ITME 8·9·10쪽)와 2026-09-22에 확인한 arXiv 초록(2402.02750v2, 2606.12556v2)만 요약했습니다. 설계서 A.2의 ITME "vLLM 구현·NVMe-oF 대비 1.80배"는 원문 페이지를 대조하지 못해 근거에 넣지 않았습니다.

| 사례 | 근거 | accepted 판정 (요약) | disputed (사람 검토) |
| --- | --- | --- | --- |
| `kivi_paper` | KIVI p.1·2·6·8 요약 | cost 적합/조건부, performance·quality·scalability 조건부, operations 확인 불가 | cost·performance·quality·scalability의 확인 불가, operations 조건부 |
| `itme_paper` | ITME p.1·8·9·10 요약 | cost 확인 불가, performance·operations·scalability 조건부, quality 확인 불가 | cost 조건부, operations 확인 불가/부적합(설계서 해석 쟁점), performance·scalability 확인 불가 |
| `context_only` | 분야 배경 가짜 자료(technology=other, scope=context) | 전 항목 확인 불가 | 없음 |
| `no_evidence` | 없음 | 전 항목 확인 불가 | 없음 |

accepted 밖의 판정과 필수 근거 미인용, 도메인 규칙 위반은 `fail`, disputed는 `inconclusive`입니다. 애매한 경계는 단일 정답으로 만들지 않았습니다.

```bash
JUDGE_MODEL=gpt-4.1-mini uv run python -m app.run_node --node domain --mode fixture --input tests/fixtures/domain/fit_eval/kivi_paper.json
uv run python -m app.evaluate_domain_fit --case kivi_paper --run outputs/local/<위 실행 디렉터리>
uv run python -m app.evaluate_domain_fit --case kivi_paper --run outputs/local/<위 실행 디렉터리> --draft
uv run python -m app.run_node --node domain --mode fixture --case acceptance
uv run python -m app.check_handoff --node domain --result outputs/local/<실행 디렉터리>/result.json
```

종료 코드는 `0=pass`, `2=fail/inconclusive`, `1=입력 오류`입니다. mock 실행은 항상 `fail`입니다. 이 사례는 도메인 판정 로직의 회귀 점검이며 RAG 검색 품질 측정값이 아닙니다.

## 5. 남은 간극

- 외부 적용 사례(web) 근거가 없으므로 performance 적합은 현재 어떤 사례에서도 나올 수 없습니다. 실제 웹 검색 결과와 `data/source_annotations.yaml`의 사람 검토가 필요합니다.
- 규모 차이·상충 보존·기준선 표현 등 rationale의 의미 검사는 `manual_review`로 남아 있습니다. 6절의 ITME 실행에서 35.7%의 기준선을 초록(CPU 오프로딩 대비)이 아니라 p.9의 “GPU 메모리 및 오프로딩 기준”으로 옮긴 claim을 mini Judge가 supported로 통과시켰습니다. 인용 Judge는 기준선 혼동을 잡지 못합니다.
- cost·operations의 부적합 조건은 설계서 표 10과 다르게 적용했습니다. 팀 합의가 바뀌면 rubric 정의와 `labels.json`의 disputed 항목을 함께 바꿔야 합니다.
- 같은 입력이라도 Generator 초안이 실행마다 달라집니다(temperature 0에서도 ITME performance가 한 번은 조건부 적합, 한 번은 확인 불가). 회귀 점검은 여러 번 실행해 범위를 봐야 합니다.

## 6. 실제 실행 결과 (2026-09-22, gpt-4.1-mini Generator, fixture 모드)

프롬프트를 두 차례 조정하고 draft 규칙·수정 우선순위를 넣은 뒤의 최종 실행입니다. 근거는 검토자 요약이므로 아래 판정은 **판정 로직 점검 결과**이며 기술 평가 결론이 아닙니다.

| 사례 | Judge | 전달 결과(result.json) | 채점 | 비고 |
| --- | --- | --- | --- | --- |
| `itme_paper` | gpt-4.1-mini | cost 확인 불가, performance 조건부, quality 확인 불가, operations 조건부, scalability 조건부 | **pass** | 4 claim 모두 supported, 검증 오류 없음 |
| `kivi_paper` | gpt-4.1-mini | cost 확인 불가, performance 조건부, quality 조건부, operations 확인 불가, scalability 확인 불가 | inconclusive | cost·scalability의 확인 불가는 disputed(사람 검토). 검증 오류 없음 |
| `context_only` | gpt-4.1-nano | 전 항목 확인 불가, claim 0건 | pass | 배경 자료를 직접 근거로 쓰지 않음 |
| `no_evidence` | gpt-4.1-nano | 전 항목 확인 불가, `needs_revision` | pass | 충분성 Judge 가드 전에는 `failed`였음 |
| `kivi_paper` | gpt-4.1-nano | 전 항목 보류 | fail | 수정 1회 후에도 conditions에 “미상”이 남았고, nano Judge가 정확한 긍정문 claim도 “rubric에 필요한 수치가 없다”는 이유로 unsupported 처리 |
| `itme_paper` | gpt-4.1-nano | operations 조건부, 나머지 확인 불가 | inconclusive | Generator가 claim을 1개만 작성. “설정 변경 수준으로 도입 가능”이라는 근거 없는 문장을 nano Judge가 supported 처리 |
| `acceptance` (demo 근거) | gpt-4.1-mini | 10항목 모두 확인 불가, claim 0건 | — | `check_handoff`는 demo fixture가 최종 출처가 될 수 없다는 문제만 보고. 실제 출처로 바꾸면 인수 형식 충족 가능 |

관찰:

- 인용 Judge 모델이 결과를 좌우합니다. 같은 초안에 대해 `gpt-4.1-nano`는 긍정문 사실 claim 5개 중 4개를 unsupported로 판정했고, `gpt-4.1-mini`는 5개 모두 supported였습니다. 공통 런타임은 unsupported가 하나라도 있으면 모든 판정을 보류하므로 nano로는 도메인 결과가 거의 항상 전부 확인 불가가 됩니다. 도메인 노드 실행 시 `.env`의 `JUDGE_MODEL=gpt-4.1-mini` 사용을 권장합니다. `config.yaml` 기본값 변경은 팀 결정 사항입니다.
- Generator는 지시에도 불구하고 “~하였으나 …않았다” 형태의 부재 절을 claim에 넣는 경향이 있습니다. draft 규칙과 수정 루프가 이를 한 번 되돌리며, 그래도 남으면 결과가 보류됩니다.
- 판정 항목의 rationale은 규모 차이와 “공개 정보 기반 추정”을 포함했고, 자사 논문 편중과 설계서 표 10 해석 검토 대상이 limitations에 남았습니다.
