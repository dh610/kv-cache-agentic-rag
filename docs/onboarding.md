# 팀원 시작 안내

## 최초 한 번

1. README의 기초 환경 브랜치를 clone하고 `uv sync --frozen`을 실행합니다.
2. `.env.example`을 `.env`로 복사합니다. 이미 `.env`가 있으면 덮어쓰지 않습니다.
3. 키 없이 `uv run python -m app.run_node --node 본인노드 --mode mock`으로 설치를 확인합니다.
4. 개인 OpenAI 키를 넣습니다. 추적을 사용할 때 LangSmith 키와 본인 프로젝트 이름도 넣습니다.
5. `--mode fixture`로 실제 LLM 결과를 확인합니다. LangSmith는 LLM 사용료를 대신 지불하지 않습니다.

노드 이름: `tech`, `market`, `stakeholder`, `domain`, `synthesis`, `report`.
LangSmith workspace를 지정해야 하는 계정은 `.env.example`의 `LANGSMITH_WORKSPACE_ID`를 설정합니다.
EU/자체 호스팅은 `LANGSMITH_ENDPOINT`를 본인 환경에 맞춥니다.

## 매일 수정하는 파일

| 경로 | 담당 내용 |
| --- | --- |
| `prompts/<node>/system.j2` | 노드의 지시·추론·출력 규칙 |
| `prompts/<node>/user.j2` | 질문·근거·상위 결과 배치 |
| `rubrics/<node>.yaml` | 기준 ID, 정의, 허용 판정 |
| `tests/fixtures/<node>/basic.json` | 담당 노드의 질문과 고정 근거 |
| `tests/fixtures/<node>/missing-evidence.json` | 근거가 없을 때의 처리 확인 |

Jinja에서 사용할 수 있는 변수는 `node`, `rubric`, `input_json`, `evidence_json`입니다.
존재하지 않는 변수는 즉시 오류가 납니다. `prompts/shared`는 모든 노드에 영향을 줍니다.
질문은 최대 6개가 기본값이며 rubric에 정의된 criterion을 사용해야 합니다.
더 풍부한 고정 입력은 각자 별도 파일을 만들고 지정할 수 있습니다.

```bash
uv run python -m app.run_node --node domain --mode fixture --input tests/fixtures/domain/my-case.json
uv run python -m app.run_node --node domain --mode mock --case missing-evidence
```

두 번째 명령은 `needs_revision`과 종료 코드 2가 정상입니다.
개인·비공개 자료가 담긴 입력은 `data/private/`에 보관하고 `--input`으로 전달하세요.
공유 fixture에는 팀이 공개 저장소에 올려도 되는 최소한의 근거만 넣습니다.

## 결과 확인

`outputs/local/<실행 ID>/result.json`에서 `status`, `validation_errors`, `result.unverified`, `checks`를 확인합니다.
`system.txt`, `user.txt`는 실제 마지막 생성에 사용한 렌더링 결과입니다.
`run.json`에 모델 설정, git revision/dirty 상태, 실행 ID, 추적 설정과 trace 링크가 남습니다.
전체 파이프라인은 `state.json`과 개발용 `preview.md`를 저장합니다.
동일 파일을 덮어쓰지 않도록 매 실행에 고유 디렉토리를 만듭니다.

fixture와 mock의 차이:

- mock: 네트워크·키 불필요. `.j2` 문법과 그래프 연결을 확인하되 프롬프트 내용을 LLM이 해석하지 않습니다.
- fixture: 고정 근거로 실제 LLM/Judge 호출. 프롬프트 수정 효과를 비교하는 모드입니다.
- 기본 발췌는 최종 평가셋이 아닙니다. 모델이 없는 정보를 만들어내지 않고 `확인 불가`를 반환하는지도 확인하세요.
- LLM Judge가 supported라고 한 사실만으로 기술의 진실성·평가 기준의 타당성이 증명되지는 않습니다.

## 문서와 실제 검색

`data/papers/kivi.pdf`, `data/papers/itme.pdf`에 공유 ZIP의 원문을 놓습니다.
PDF 원문은 Git에 올리지 않습니다. 다른 팀원도 같은 판본을 준비하고 `app.index --check`의 signature를 비교할 수 있습니다.
문서를 추가하면 `data/documents.yaml`에도 ID·원문 링크·기술·target/reference·본문 범위를 등록하세요.
페이지 범위는 PDF 물리 페이지 기준이며 전체 파일 페이지 합계도 200을 넘을 수 없습니다.
기존 문서의 메타데이터 변경은 담당자 간 공유하고, 각자 로컬 인덱스를 다시 빌드합니다.

```bash
uv run python -m app.index --check
uv sync --frozen --extra rag
uv run --extra rag python -m app.index
uv run --extra rag python -m app.run_node --node domain --mode rag
uv run python -m app.run_node --node stakeholder --mode web
```

`rag`는 논문만, `web`은 웹만 사용하므로 개별 어댑터를 따로 점검할 수 있습니다.
전체 `pipeline --mode live`에서는 기술=논문, 시장·도메인=논문+웹, 이해관계자=웹을 사용합니다.
시장·도메인의 두 검색 제공자 중 하나가 실패하면 그 검색 시도는 실패로 기록합니다.
실제 검색은 질문별 최대 3회, 매 라운드 Generator/Judge 호출이 생깁니다. 첫 확인은 fixture 한 노드로 시작하세요.

## Git 작업 절차

기반 브랜치가 main에 병합된 이후의 일반 작업 예시입니다.

```bash
git switch main
git pull --ff-only
git switch -c feat/market-prompt-본인이름
# 담당 파일 수정
uv run python -m app.run_node --node market --mode mock
uv run ruff check .
uv run pytest -q
git status --short
git add prompts/market rubrics/market.yaml tests/fixtures/market
git diff --cached
git commit -m "Improve market adoption prompt and fixtures"
git push -u origin feat/market-prompt-본인이름
```

PR에는 수정한 질문/기준, fixture 결과, 실제 LLM 확인 여부를 적습니다.
개인 브랜치에서 작업하고 shared state·schema·의존성·공통 prompt는 기반 담당자와 함께 검토합니다.
프롬프트 수정만으로 `pyproject.toml`, `uv.lock`을 변경하지 않습니다.
필요한 의존성은 `uv add`로 추가해 두 파일을 함께 PR에 넣습니다. lock 충돌은 손으로 편집하지 말고 병합한 pyproject에서 `uv lock`으로 재생성합니다.
API 키, PDF, FAISS, 모델 캐시, 결과 파일은 `.gitignore`로 제외됩니다. 공유 fixture·문서 목록·프롬프트·lock은 추적합니다.
최신 main을 브랜치에 반영한 뒤 테스트하고 PR을 합칩니다. 공유 브랜치의 force-push는 피합니다.

현재 예시는 담당자의 GitHub 계정을 가정하지 않습니다. 팀원 초대와 CODEOWNERS는 실제 계정·역할 확정 후 설정하면 됩니다.
