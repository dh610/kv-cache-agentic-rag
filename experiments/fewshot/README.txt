판단 논거와 few-shot의 생성 품질·수정 비용 비교

윤동현의 성능 실험. 생산 프롬프트와 공통 실행 코드는 수정하지 않는다.
domain/stakeholder 담당자의 기존 프롬프트 위에 실험 프로세스 안에서만
가상 기술의 근거 → 해석 → 판정 사례를 덧붙인다. 기존 rubric, 인용 Judge,
입출력 계약, 실패 전파를 유지한다. 실제 기술 사실과 예시를 혼합하지 않는다.
해당 역할 담당자와 공유 실행기 담당자의 검토 후에만 생산 적용을 결정한다.

입력은 기존 live 실행 state.json에 보관한 실제 원문 전체이며 acceptance의
질문 목록과 기술 조사 결과를 결합한다. 데모 근거가 섞이면 실행을 거부한다.
각 역할의 A/B는 같은 input JSON/hash를 사용한다. 추가 웹 검색은 하지 않는다.
fixture 모드에서 실제 OpenAI 모델, 충분성 검사, 생성, 원래의 순차 인용 Judge,
최대 1회 표현 수정까지 실행한다. 재검색과 전체 supplement를 측정하지 않으므로
전체 보고서 속도 개선 또는 검색 개선을 입증하는 실험으로 해석하면 안 된다.
fixture 모드는 실제 live의 기술별 생성 분할과 입력 선별 경로도 다르다.

통제: generator/judge gpt-4.1-mini, temperature=0, 개별 호출 timeout=60초,
SDK retry=0. 두 조건에 동일하게 적용한다. 역할별 AB → BA 두 번 반복한다.
생성 프롬프트만 바뀌고 Judge는 고정하므로 판정 기준 완화와 혼동하지 않는다.
전체 child 실행은 300초 제한이다. preflight가 실패하면 유료 실험을 진행하지 않는다.

실행 예시 (실제 API 비용 발생):
uv run python -m app.benchmark_fewshot --out outputs/local/fewshot-NEW-ID \
  --source-state outputs/local/ARCHIVED-LIVE-RUN/state.json \
  --key-file ~/.gpt-key --repeats 2

LangSmith 키와 개인 프로젝트는 기존 환경/.env에서 읽는다. 키를 출력하지 않는다.
--key-file은 명시적으로 선택한 키 파일을 읽어 child 환경에만 전달한다.
출력 디렉터리는 새 경로여야 하며 기존 결과를 덮어쓰지 않는다.

산출물: protocol.json, 입력 JSON, run별 result/draft/system/user/summary,
단계 이벤트와 progress.jsonl, LangSmith URL, comparison.html/json.
확인 불가를 알려진 판정으로 세지 않는다. 미확인·검증 오류를 삭제하지 않는다.
초기 verify 판정, fix 수, 알려진 평가 수, 주장 수, 단계 시간, 토큰, 모델 오류,
가상 예시 이름 유출을 함께 본다. 줄어든 출력량을 무조건 품질 개선으로 보지 않는다.
LLM Judge의 통과는 사실성의 최종 승인이 아니며 원문을 직접 대조해야 한다.
반복 2회의 소규모 결과이므로 평균 개선을 일반적인 성능 보장으로 제시하지 않는다.
