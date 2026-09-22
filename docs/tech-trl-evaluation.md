# 기술 조사 TRL 평가 사례

공통 런타임과 채점기는 `runtime/validation.py`의 검사를 공유합니다. 필수 TRL 근거는 claim뿐 아니라 해당 claim의 유일한 supported Judge 검사에도 포함되어야 합니다. 제공된 `trl_estimates`와 기술 노드의 `maturity.judgment`가 다른 경우(확인 불가/null 불일치 포함) 실패합니다. 기존 사례처럼 빈 `trl_estimates`는 허용합니다. 입력·반환 JSON 구조는 변경하지 않았습니다.

`tests/fixtures/tech/trl_eval/`에는 KIVI와 ITME 원문을 검토해 만든 두 개의 근거 제한 사례와 근거 없음 사례가 있습니다. `evidence.text`는 **원문을 그대로 복사한 발췌가 아니라 검토자의 요약**이며 `source_type=fixture`입니다. 원문 페이지와 URL을 함께 남겼지만, 이 입력이나 채점 결과를 최종 기술 판정 또는 보고서의 원문 인용으로 사용하지 않습니다.

검토 출처:

- KIVI, arXiv:2402.02750v2, PDF 2·6·8쪽. 2쪽은 비대칭 양자화와 잔여 캐시, 6쪽은 품질 실험 및 Falcon 제약, 8쪽은 A100·ShareGPT 기반 효율 실험을 확인했습니다.
- ITME, arXiv:2606.12556v2, PDF 8·9·10쪽. 8쪽의 FPGA 기능 시제품과 별도 CMM 성능 플랫폼, 9쪽의 Llama-3.1·ShareGPT 워크로드, 10쪽의 FPGA 비교를 확인했습니다.

`labels.json`의 단계 범위는 **제공된 근거로 주장할 수 있는 잠정 범위**입니다. KIVI 3~5, ITME 4~6은 이 논문에서 확인한 실험·시제품의 해석 범위이며 현재 기술의 최종 TRL이 아닙니다. 단계 경계가 애매한 사례는 `확인 불가`로 보류할 수 있지만, 채점기는 이를 `inconclusive`로 보고합니다. 근거 없는 사례에서만 `확인 불가`가 `pass`입니다. 채점기는 TRL 판정에 실험·시제품 필수 근거 ID가 연결됐는지도 확인합니다. `pass` 역시 인용 연결과 허용 범위를 통과했다는 뜻이며 `manual_review`의 실험 조건·원문 대조는 별도로 필요합니다.

실제 Generator/Judge 호출은 사용자가 키를 설정하고 비용을 감수해 실행할 때만 합니다. 사례별 실행:

```bash
uv run python -m app.run_node --node tech --mode fixture --input tests/fixtures/tech/trl_eval/kivi_paper.json
uv run python -m app.evaluate_tech_trl --case kivi_paper --run outputs/local/<위 명령의 실행 디렉터리>
```

다른 사례도 `itme_paper`, `no_evidence`로 같은 방식으로 실행합니다. 채점기는 저장된 `input.json`이 선택한 사례와 같은지 확인하며, `result.json`을 오프라인에서 검사합니다. 종료 코드는 `0=pass`, `2=fail/inconclusive`, `1=입력 오류`입니다. `no_evidence`의 노드 실행은 설계대로 `needs_revision`이며 종료 코드 2일 수 있지만 결과 디렉터리는 생성됩니다.

이 사례는 **기술 조사 판정 로직의 회귀 점검**을 위한 것입니다. 논문 검색 Hit@5·MRR과 별개이며, 원문 PDF를 로컬에 두거나 인덱스를 만들 필요가 없습니다. 실제 기술 평가에는 검색으로 얻은 논문 원문과 최신 채택 자료를 사람이 대조해야 합니다.
