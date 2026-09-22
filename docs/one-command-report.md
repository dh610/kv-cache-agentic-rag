# 처음 clone한 환경에서 보고서 생성

macOS, Linux 또는 Windows의 WSL에서 실행합니다. 네트워크 연결이 필요하며 uv가 없는 경우 curl이 필요합니다. Native Windows PowerShell은 이 셸 실행기의 지원 범위가 아닙니다.

## 1. 키만 설정

```bash
git clone https://github.com/dh610/kv-cache-agentic-rag.git
cd kv-cache-agentic-rag
cp -n .env.example .env
```

`.env`에 `OPENAI_API_KEY`, `TAVILY_API_KEY`를 입력합니다. 이미 export한 환경변수는 `.env`보다 우선합니다. 실행기가 셸 설정 파일을 source하거나 키를 출력하지는 않습니다. `.env`는 Git에서 제외되며 기존 파일을 덮어쓰지 마세요.

LangSmith 추적은 선택 사항입니다. `LANGSMITH_TRACING=true`라면 개인 `LANGSMITH_API_KEY`와 `LANGSMITH_PROJECT`가 필요합니다. 모델 변경은 기존 `GENERATOR_MODEL`, `JUDGE_MODEL` 환경변수를 사용합니다. 개인 API 사용 가능 여부·잔액은 별도로 준비해야 합니다.

## 2. 실행

```bash
./run-report.sh
```

실행 순서는 다음과 같습니다.

1. 기존 uv를 사용하거나 저장소 `.cache/tools`에 uv 0.12.1을 설치합니다. 셸 프로필은 바꾸지 않습니다. 설치 방식은 [uv 공식 unmanaged installer](https://docs.astral.sh/uv/reference/installer/#unmanaged-installations)를 따릅니다.
2. Python 3.11이 없으면 uv가 준비하고, `uv.lock`의 의존성을 `.venv`에 설치합니다. 필수 키가 없으면 RAG 의존성·모델 다운로드 전에 중단합니다.
3. `data/paper_downloads.json`의 고정 arXiv 판본 PDF를 없을 때만 받습니다. 기존 파일은 보존하며 등록 페이지 수와 PDF 형식을 검사합니다. 다운로드는 임시 파일에서 검증 후 반영합니다.
4. PDF/메타데이터/임베딩 설정과 인덱스 서명·파일 해시·차원을 비교합니다. 현재 인덱스는 재사용하고 없거나 오래됐거나 손상됐으면 다시 만듭니다. BGE-M3는 기존 Hugging Face 캐시를 재사용하며 없으면 다운로드합니다. 최초 다운로드는 수 GB이고 CPU 인덱싱에도 시간이 걸립니다.
5. 실제 조사 → 종합 → 필요 시 최대 1회 보완 → 보고서 생성·검사를 실행합니다. 이 단계부터 OpenAI·Tavily 호출 비용이 발생합니다.

CPU 실행의 기본 `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false`로 macOS의 확인된 병렬 런타임 충돌을 피합니다. 호출자가 명시한 환경변수는 유지합니다.

## 설치와 연결만 먼저 확인

```bash
./run-report.sh --prepare-only
./run-report.sh --mock
```

`--prepare-only`는 키 없이 PDF·임베딩·인덱스를 준비하고 종료합니다. LLM·검색 API를 호출하지 않습니다. 이미 준비된 환경에서 반복하면 인덱스 생성을 건너뜁니다.

`--mock`은 기본 의존성만 준비하고 PDF·임베딩 다운로드 없이 고정 근거로 보고서를 만듭니다. 이는 연결 확인용이며 실제 기술 평가나 검색 성능 결과가 아닙니다.

## 결과와 종료 코드

터미널에 `Report PDF`, `Report Markdown`, `Results` 경로가 표시됩니다. 결과는 매번 새로운 `outputs/local/<실행 ID>/`에 저장됩니다.

- `report.pdf`, `report.md`: 생성된 보고서 초안
- `state.json`: 노드 결과, gaps, `report_check.ready`와 보완 사유
- `run.json`: 실행 설정·추적 메타데이터

종료 코드 `0`은 해당 모드의 정상 완료, `2`는 보완 필요, `1`은 실행·설정 실패입니다. 초안 파일이 있어도 ready=false이면 제출 완료가 아닙니다. 2를 성공으로 바꾸거나 확인 불가 항목을 자동으로 제거하지 않습니다.

## 준비 단계가 멈출 때

- 키 누락: 안내된 이름을 `.env` 또는 export로 설정하고 같은 명령을 다시 실행합니다. LangSmith만으로 LLM을 호출할 수는 없습니다.
- arXiv 접근 실패/판본 차이: 오류에 나온 고정 판본을 `data/papers/<이름>.pdf`로 직접 저장합니다. 기존 파일이 잘못됐다면 별도 보관 후 올바른 판본을 준비합니다. 팀의 본문 페이지 범위를 임의로 바꾸지 마세요.
- 모델 다운로드 실패: 네트워크·디스크 공간을 확인하고 재실행합니다. Hugging Face 개인 캐시는 유지됩니다. 공개 BGE-M3 모델을 위해 토큰을 입력하는 단계는 없습니다.
- PDF 또는 메타데이터 변경: 재실행하면 인덱스를 다시 만듭니다. 새 논문을 등록할 때는 documents.yaml과 paper_downloads.json을 함께 갱신합니다.
- 초기 설치 실패: 해당 운영체제·아키텍처에서 Python 3.11/RAG 의존성 wheel 지원과 네트워크를 확인합니다. 시스템 관리자 권한이나 전역 패키지 설치는 사용하지 않습니다.

## 검증 범위

단위 테스트는 잘못된 PDF·판본 거부, 기존 파일 보존, 키 누락 시 조기 종료, 손상·변경 인덱스 감지, mock/설치 전용 분기를 검증합니다. 실제 LLM이 내리는 평가와 보고서 원문 인용의 의미적 정확성은 별도 검토 대상입니다.
