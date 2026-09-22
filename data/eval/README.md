# 검색 평가 입력

`retrieval_qa.json`은 담당자가 KIVI/ITME 원문과 대조해 만들 30문항입니다. 검증하지 않은 정답이나 성능을 완성된 것으로 배포하지 않습니다.
각 기술 15문항, 한국어 질문, 약어/수치가 있는 문항 10개 이상, 원문 근거 문장과 영어 핵심어를 준비합니다.

JSON 배열의 항목:

```json
{
  "id": "kivi-01",
  "technology": "KIVI",
  "question": "KIVI에서 key와 value의 2bit 양자화 단위는 각각 무엇인가?",
  "english_keywords": "KIVI 2bit per-channel key per-token value",
  "document_id": "kivi",
  "page": 1,
  "answer_quote": "원문에서 직접 대조한 근거 문장으로 교체",
  "verified": false,
  "acronym_or_number": true
}
```

페이지는 PDF 물리 페이지입니다. `verified=true`는 사람이 원문과 대조한 뒤에만 설정합니다.
위 예시는 실제 정답 라벨이 아닙니다. 짧고 완결된 근거 문장을 사용합니다.
청킹 설정마다 원문 문장에서 chunk ID를 다시 도출합니다. 어느 청크에도 해당 문장이 온전히 없으면 실험을 실패로 기록합니다.
자료를 공개 저장소에 올릴 권한이 없으면 `data/private/retrieval_qa.json`에 보관합니다.

```bash
# 입력 구조 검사. 아직 모델을 받거나 성능을 측정하지 않음.
uv run python -m app.evaluate_retrieval --dataset data/private/retrieval_qa.json
# 실제 3모델 비교. RAG 추가 의존성과 모델 다운로드가 필요함.
uv sync --frozen --extra rag
uv run --extra rag python -m app.evaluate_retrieval --dataset data/private/retrieval_qa.json --run --remediate
```

모델의 사용자 정의 코드가 필요하면 기본 실행은 실패로 기록합니다. 코드를 검토한 뒤에만 `--trust-remote-code`를 명시합니다.
후보 모델 일부가 실패하면 나머지만으로 최종 선정 완료를 선언하지 않습니다.

MRR 차이 0.05 이내 후보 중 Hit@1, 파라미터 수 순으로 결정합니다. 이는 합의된 선택 규칙이지 통계적 유의성 증명이 아닙니다.
MRR은 필터링된 전체 순위(리랭커는 후보 풀) 기준이며 Hit@1/3/5도 함께 기록합니다. MRR@5와 혼용하지 마세요.
통과 조건: Hit@5 >= 0.80, MRR >= 0.60. 미달 시 청킹 -> 이중언어 -> BGE-M3 learned sparse RRF -> 리랭커 순서로 재측정합니다.
동일 평가셋을 사용한 개발·선택 결과이므로 별도의 보지 않은 질문에 대한 일반화 성능을 증명하지는 않습니다.

결과는 `outputs/local/retrieval-eval-*.json`에 설정, 데이터 hash, 모델 revision, 문항별 순위와 지표를 남깁니다.
프로덕션 `config.yaml`을 자동으로 바꾸지 않습니다. 선택한 모델/청킹/검색 방식을 설정·구현에 반영하는 PR과 인덱스 재생성이 필요합니다.
