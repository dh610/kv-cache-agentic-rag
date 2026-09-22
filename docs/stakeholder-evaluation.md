# 이해관계자 평가 사례

`tests/fixtures/stakeholder/eval/`에는 이해관계자 노드의 판정 로직을 점검하는 여섯 사례가 있습니다.
각 사례는 하나의 (technology, criterion)만 묻고, `evidence.text`는 **원문을 그대로 복사한 발췌가 아니라 검토자가 실제 공개 페이지를 읽고 쓴 한국어 요약**이며 `source_type=fixture`입니다.
URL·사이트·발행일은 실제 페이지에서 확인한 값만 적었고, 확인하지 못한 발행일은 비워 두었습니다. 이 입력이나 채점 결과를 실제 이해관계자 여론, 최종 판정, 보고서의 원문 인용으로 사용하지 않습니다.

| 사례 | 항목 | 기대 | 확인하는 규칙 |
| --- | --- | --- | --- |
| `adopter_third_party` | KIVI/adopters | 중립 또는 긍정 | 제3자 개발자 블로그(설정 가이드·정확도 비용)는 도입 기업·개발자 근거다 |
| `industry_approach_level` | ITME/industry | 긍정 또는 중립, 단 claim.conditions에 "접근 전반" | 기사가 ITME를 이름으로 말하지 않으면 접근 전반 근거로 표시해야 한다(설계서 §A.4, 프롬프트 규칙 7) |
| `industry_repost_trap` | KIVI/industry | 확인 불가만 통과 | 논문 게시·요약 사이트는 이해관계자가 아니다(규칙 7-1) |
| `competitor_third_party_trap` | KIVI/competitors | 확인 불가만 통과 | "Google과 무관"이라고 밝힌 비교 사이트는 경쟁 진영이 아니다(규칙 10-3) |
| `competitor_self_report_trap` | ITME/competitors | 확인 불가만 통과 | 저자가 경쟁 기술을 평가한 문장은 경쟁 진영의 반응이 아니다(규칙 10-1) |
| `no_evidence` | ITME/competitors | 확인 불가만 통과 | 근거 없이 등급을 만들지 않는다 |

`labels.json`의 `allowed_judgments`는 **제공된 근거로 주장할 수 있는 잠정 범위**입니다. 함정(trap) 사례에서 어떤 등급이든 부여하면 `fail`, 등급 가능한 사례에서 `확인 불가`로 보류하면 `inconclusive`로 보고합니다.
채점기는 판정 범위 외에도 인용 ID가 제공된 근거 안에 있는지, 같은 기술의 근거인지, 검토자가 지정한 필수 근거를 인용했는지, 확인 불가가 아닌 판정에 supported claim이 있는지, claim의 「」 인용문이 근거 본문에 그대로 있는지(`app.evaluate_stakeholder.grounding_errors`, 생략 부호로 이은 조각과 마크다운 서식 차이는 허용)를 확인합니다. 이 검사는 이해관계자 하네스 안에서만 수행하며 공용 실행기는 바꾸지 않습니다.
`pass`는 인용 연결과 허용 범위를 통과했다는 뜻이며, `manual_review`의 발언 주체·입장·자사 발표 구분은 별도로 읽어야 합니다.

실제 Generator/Judge 호출은 사용자가 키를 설정하고 비용을 감수해 실행할 때만 합니다. 사례별 실행:

```bash
uv run python -m app.run_node --node stakeholder --mode fixture --input tests/fixtures/stakeholder/eval/adopter_third_party.json
uv run python -m app.evaluate_stakeholder --case adopter_third_party --run outputs/local/<위 명령의 실행 디렉터리>
```

다른 사례도 `industry_approach_level`, `industry_repost_trap`, `competitor_third_party_trap`, `competitor_self_report_trap`, `no_evidence`로 같은 방식으로 실행합니다.
채점기는 저장된 `input.json`이 선택한 사례와 같은지 확인하며, `result.json`을 오프라인에서 검사합니다. 종료 코드는 `0=pass`, `2=fail/inconclusive`, `1=입력 오류`입니다.
함정 사례와 `no_evidence`의 노드 실행은 설계대로 `needs_revision`(종료 코드 2)이지만 결과 디렉터리는 생성됩니다.

이 사례는 **이해관계자 판정 로직의 회귀 점검**을 위한 것입니다. 웹 검색(Tavily)의 회수율과는 별개이며 API 키 없이도 `tests/test_stakeholder_eval.py`로 채점기 자체를 검증합니다.
실제 이해관계자 평가에는 웹 검색으로 얻은 페이지 원문과 발언 주체를 사람이 대조해야 합니다.
