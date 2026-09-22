# 구현 범위와 설계 선택

## 공유 자료와의 관계

팀이 공유한 설계 PDF와 초기세팅 ZIP에서 기술 선택(KIVI/ITME), 클라우드 도메인,
LangGraph 역할, BGE-M3/FAISS, 기본 모델과 State 결과 키를 계승했습니다.
이 저장소의 기준은 사용자와 논의한 간소화안입니다. 공유 PDF 전체 기능을 구현했다고 가정하지 않습니다.

- 적용: 기술 뒤 시장·이해관계자·도메인 병렬 평가, 합류 뒤 종합·보고서 구성
- 적용: 기술·시장·도메인의 논문 RAG, 시장·이해관계자·도메인의 웹 근거
- 적용: 역할별 Jinja2 프롬프트와 rubric, 개인 LangSmith, 고정 근거 단독 테스트
- 적용: 질문별 최대 3회 검색, 실제 인용 검증, 원문 evidence를 보존하는 합류
- 제외: 별도 답변 수정 루프, 종합 뒤 자동 보완 루프 (`fix=0`, `supplement=0`으로 제한)
- 미완성: 최종 평가 기준 합의, 검색 품질 평가셋/실측, 최종 보고서 PDF

기본 rubric은 팀이 기준 정의를 편집할 출발점입니다. 교수님이 확정한 기준이나 자동 TRL 점수표가 아닙니다.
근거 개수로 시장성을 판정하거나 확인 불가를 최하점으로 치환하지 않습니다.
기존 ZIP의 미검증 평가셋·성능 수치·예시 보고서는 검증된 결과로 이관하지 않았습니다.

## 노드 내부 흐름

```mermaid
flowchart TD
    A[모든 질문의 근거 검색] --> B[담당 j2로 생성]
    B --> C[Judge와 공통 계약 검사]
    C -->|근거 부족·unsupported, 검색 예산 남음| A
    C -->|완료 또는 예산 소진| D[검증 상태와 결과 저장]
```

fixture/mock은 이미 고정된 근거이므로 재검색하지 않습니다.
실제 검색의 재질의는 시도 횟수별 고정 보조 검색어를 붙이는 기본 구현입니다.
LLM이 질문을 자유롭게 다시 만드는 query rewriting, 별도 sufficiency 모델, 자동 수정 루프는 없습니다.
misstated 및 계약 위반은 성공으로 처리하지 않고 수정 필요 상태에 남깁니다.

전체 live에서 논문 임베더와 FAISS는 한 번 로드해 공유하고 임베딩 호출은 lock으로 보호합니다.
각 병렬 노드가 같은 State 필드를 덮어쓰지 않도록 결과 슬롯을 분리했습니다.
추적에는 그래프 단계, LLM 입력/출력, retriever 결과가 연결되며 실행별 UUID를 사용합니다.

## 검색 구현

- `EvidenceSource.search(Question, attempt) -> list[Evidence]` 계약으로 고정 근거·논문·웹을 교체합니다.
- 문서 목록/판본 내용/청킹 설정/모델 설정을 hash해 오래된 인덱스 사용을 막습니다.
- PDF 물리 페이지 경계를 유지한 1,200자/200자 overlap 청킹입니다. 표·수식·그림·OCR 복원은 하지 않습니다.
- PDF 전체 페이지는 200 이하. 기본 본문 범위는 공유 ZIP의 KIVI 1–9, ITME 1–11을 계승했습니다.
- 모델의 resolved revision과 FAISS/메타데이터 hash를 인덱스 manifest에 남깁니다.
- BGE-M3의 dense 벡터만 정규화해 FAISS 내적으로 검색합니다. sparse/multi-vector 점수 융합은 없습니다.
- 작은 수업 corpus를 전제로 전체 후보에서 기술/문서 역할 필터를 적용한 뒤 top-k를 선택합니다.
- 기술 노드는 해당 기술의 target 논문만, 시장/도메인은 해당 target과 reference 문서를 허용합니다.
- 선택적 reranker의 성능·메모리 요구·판본은 아직 측정하지 않았습니다. 기본값은 꺼짐입니다.
- 웹은 Tavily가 반환한 raw_content만 evidence로 사용하고 URL/수집 시각을 남깁니다. 원문 없는 snippet은 제외합니다.

인용 Judge는 evidence에 대한 claim 정합성을 확인합니다. 외부 진실 검증, 기준 등급의 타당성,
웹 검색의 균형성, 모든 요약 문장의 정확성을 보장하지 않습니다. 최종 평가에는 팀원의 원문 검토가 필요합니다.

## 운영 범위

이 환경은 로컬 CLI와 GitHub CI용입니다. 서버 배포, UI, 지속 체크포인트, 사용자 인증은 구현하지 않았습니다.
개인 실행 결과와 키는 공유하지 않으며 Git에는 재현 가능한 코드·프롬프트·fixture·lock만 올립니다.
공통 코드와 라이브러리 업그레이드는 기반 담당자 중심으로, 프롬프트/자료 보강은 노드 담당자 중심으로 진행합니다.

설정/추적 방식은 [LangGraph tracing](https://docs.langchain.com/langsmith/trace-with-langgraph),
[구조화 출력](https://docs.langchain.com/oss/python/langchain/structured-output),
[uv 프로젝트 안내](https://docs.astral.sh/uv/guides/projects/)를 참고했습니다.
