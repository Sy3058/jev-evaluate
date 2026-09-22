# JEV 모델 평가실

네 질문 유형(코딩, 강의·첨부자료, 일반지식·설명, 추론·문제해결)의 답변을 다섯 축으로 평가하는 로컬 앱입니다.
지시사항 준수는 질문 유형이 아닌 모든 답변의 공통 평가 항목입니다.

현재 상태: **사람 검토용 평가 보조 도구. 독립적인 신뢰성 검증은 미완료입니다.**
진행 상태는 [LEDGER.md](LEDGER.md), 기준은 [RUBRIC.md](RUBRIC.md)를 확인하세요.

## 실행 (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe server.py
```

브라우저에서 http://127.0.0.1:8787 에 접속합니다. Python 3.12 이상이 필요합니다.
`.env.example`을 참고해 `.env`에 TYPESAFE_API_KEY를 설정합니다. 키는 서버에서만 사용하며 화면/CSV에 내보내지 않습니다.
기본 JEV 버전은 `jev-1.13.0`으로 고정합니다. `--model`로 변경할 수 있으며 반환된 실제 버전도 저장합니다.
브라우저 의존성을 설치하지 않아도 텍스트 평가는 가능하지만 HTML 시각 평가가 판정 불가로 표시됩니다.

## 사용 흐름

1. 질문 유형과 프롬프트, 필요하면 대화·첨부 원문을 입력합니다.
2. 사용자 질문이 모든 모델에 적용할 공통 요구사항입니다. 별도 입력이나 확인 체크가 필요하지 않습니다.
3. 필요할 때만 출력 형식·기준 답안·1차 출처·자동 검사 또는 ‘추가 평가 기준’을 등록합니다.
4. 모든 모델 답변을 같은 케이스에 저장합니다.
5. 결과 화면에서 평가합니다. 항목별 단계와 근거, 실제 검사 범위, 검토 필요 사유를 확인합니다.

평가 설정은 케이스 단위로 공유됩니다. ‘공통 설정’에서 수정하면 이전 결과에 재평가 필요 표시가 붙습니다.
이전 평가와 기준은 삭제하지 않으며 ‘이력’에서 확인할 수 있습니다. 기존 ‘지시사항 준수’ 유형은 네 유형 중 하나로 직접 재분류하세요.
미평가 일괄 실행은 이미 평가된 답변을 자동 재평가하지 않습니다. 첫 오류에서 중단하고 사유를 표시합니다.

## 점수의 의미

IF, Truthfulness, Response Length, Style & Clarity, Harmlessness/Safety를 각각 0~3 단계로 판정합니다.
UNKNOWN(판정 불가), NA(해당 없음)는 점수가 없으며 평균에 0 또는 만점으로 넣지 않습니다.
NA는 사실 주장이 없는 Truthfulness에 사용합니다. 무해한 답변의 Safety는 충족으로 판정합니다.
confidence는 JEV 판단의 확신도이며 품질 점수나 실제 정확도가 아닙니다.
검증된 가중치가 없으므로 종합 점수와 전체 순위를 만들지 않습니다. 이전 확률 점수는 별도로 보존합니다.

## 근거와 검사 등록

출처 입력 예시 (입력한 발췌문만 평가에 사용하며 URL을 자동 조회하지 않음):

```json
[{"title":"공식 자료 제목","url":"https://example.org/official","text":"검증할 원문 발췌","checkedAt":"2026-09-22"}]
```

등록자는 실제 1차 출처인지 확인해야 합니다. 자료가 없는 사실 주장에는 검증 불가를 허용합니다.
첨부 원문과 일치한다는 판정은 외부 세계의 사실 정확성을 보증하지 않습니다.

검사 입력 예시:

```json
[{"kind":"json_keys","value":["answer"]},{"kind":"max_chars","value":500}]
```

지원: json, json_keys(정확한 키 집합), max_chars(공백 포함), contains, not_contains,
list_count(명시적 목록 줄 수), python_syntax, exact_answer(문자열 동일성).
검사 실패는 관련 축 판정과 함께 검토 대상으로 표시하며 임의로 점수에서 차감하지 않습니다.

Python 함수 테스트는 출력 형식을 Python으로 선택한 뒤 다음처럼 등록합니다.

```json
{"function":"add","cases":[{"args":[2,3],"expected":5},{"args":[-1,1],"expected":0}]}
```

Docker와 로컬 `python:3.12-slim` 이미지가 필요합니다. 앱은 이미지를 자동 설치하지 않습니다.
네트워크 차단, 읽기 전용 파일시스템, 비특권 사용자, CPU/메모리/시간/출력 제한을 적용하고 실제 이미지 ID를 기록합니다.
실행 환경이 없으면 `unavailable`로 표시하며 호스트 Python 실행으로 대체하지 않습니다. 테스트 통과는 등록된 사례에만 해당합니다.
현재 작업 환경에는 Docker가 없어 실제 컨테이너 실행은 미검증이며, 인터페이스/타임아웃/환경 부재 처리는 모의 테스트로 검증했습니다.

## 검증 범위와 제한

- 코딩: Python 구문 검사와 기준 답안 대조, 선택적 Docker 함수 테스트. 등록 테스트와 실행 환경이 없으면 기능 정확성 미검증으로 표시합니다. 다른 언어 및 임의 프로젝트 빌드는 미지원입니다.
- 강의·첨부자료: 원문/의미 단위 요구사항 대조. 줄 개수로 완전성을 계산하지 않습니다.
- 일반지식: 등록 근거와 비교. 자동 공식 문서 검색이나 전면적 팩트체크는 미지원입니다.
- 추론: 등록 정답·조건 대조 및 명시적 검사. 범용 계산·증명 검증은 미지원입니다.
- HTML: Chromium에서 스크립트·외부 자원을 차단하고 텍스트·숨김·가로 넘침을 관측합니다. 첫 화면 PNG는 `data/artifacts`에 저장합니다. 레이아웃 관측은 시각적 품질 판정을 대체하지 않습니다.
- 생성 시간은 수집하지 않으며 답변 텍스트로 추측하지 않습니다.
- 요구사항 최대 40개, 출처 최대 30개, 답변/근거 위치 각각 최대 200개, 입력 크기 제한이 있습니다. 초과 시 오류를 내고 조용히 잘라내지 않습니다.

실제 JEV 호출에는 프롬프트·자료·답변이 전송됩니다. 로컬 SQLite는 `data/evaluations.db`이며 `--db`로 변경할 수 있습니다.
원시 응답, 입력 스냅샷, 기준/모델 버전, 해시, 검사·근거를 저장합니다. CSV도 같은 판정/상태를 내보냅니다.

## 기능 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

브라우저 테스트는 실제 Chromium을 사용합니다. API 연동 단위 테스트는 모의 응답이므로 JEV 판단 정확성의 증거가 아닙니다.

## 실제 평가기 검증

```powershell
# 자동 검사만; JEV 미호출
.\.venv\Scripts\python.exe validate.py --split all --output validation/offline-run.json
# 고정 버전으로 조정용 사례를 3회 평가
.\.venv\Scripts\python.exe validate.py --live --split calibration --repeats 3 --output validation/calibration-new.json
# HTML/거절/주입 변형 진단
.\.venv\Scripts\python.exe validate.py --live --cases validation/metamorphic.json --repeats 3 --output validation/metamorphic-run.json
# 독립적인 사람 라벨이 준비된 뒤 최종 검증
.\.venv\Scripts\python.exe validate.py --live --split holdout --labels validation/human-labels.json --output validation/holdout-run.json
# 저장한 결과를 사람 라벨과 다시 비교 (API 호출 없음)
.\.venv\Scripts\python.exe validate.py --report-from validation/holdout-run.json --labels validation/human-labels.json --output validation/metrics.json
```

예제는 AI가 작성한 합성 진단 자료이며 독립적인 사람 정답이 아닙니다. 최종 검증용 자료는 조정에 사용하지 않습니다.
`cases.human-labels.template.json`의 reviewer, humanReviewed, axes를 사람이 작성해야 정확도 집계에 포함됩니다.
caseHash가 실행 자료와 일치해야 집계합니다. 최초 반복만 사람 일치율에 사용하고 반복 횟수로 표본 수를 부풀리지 않습니다.
유형×축별 일치율, 95% Wilson 구간, 오탐·누락·큰 오차, 판정 가능률과 반복 안정성을 별도로 보고합니다.
사람 라벨·충분한 표본·사전 합격 기준 충족 전에는 신뢰성이 검증됐다고 표시하지 않습니다.
