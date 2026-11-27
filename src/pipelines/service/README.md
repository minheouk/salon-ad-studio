# src/pipelines/service (서비스 개발)

사장님이 실제로 쓰는 **화면과 그 뒤의 처리**를 둡니다.

담당: 김도영 ([@diokim17](https://github.com/diokim17))
리뷰: PM ([@minheouk](https://github.com/minheouk))

| 폴더 | 하는 일 |
|---|---|
| `frontend/` | 화면 (입력, 결과 확인, 다운로드) |
| `backend/` | 사용자 요청 처리, 서빙 API 호출, 결과 저장 |

## 로컬 실행

저장소 루트에서 서비스 백엔드 의존성을 설치합니다.

```sh
python3 -m venv "$HOME/.venvs/onecut-nail"
source "$HOME/.venvs/onecut-nail/bin/activate"
python -m pip install -r src/pipelines/service/backend/requirements.txt
```

저장소 루트의 `.env`에 아래 값을 설정합니다. 실제 접속 문자열과 API 키는 Git에 올리지 않습니다.

| 환경 변수 | 용도 |
|---|---|
| `SUPABASE_DB_URL` | Supabase Postgres 접속 문자열 |
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase Auth에서 사용할 프로젝트 공개 키 |
| `AI_BASE_URL` | 모델 서버 기본 주소. 설정을 생략하면 개발용 예시 결과를 사용합니다. |
| `RESEND_API_KEY` | 채팅 만료 안내를 보낼 때 사용하는 Resend 발송 키 |
| `ONECUT_RETENTION_FROM` | 만료 안내 메일의 발신자 이름과 이메일 주소 |
| `ONECUT_SECURE_COOKIES` | HTTPS 운영 환경의 인증 쿠키에 `Secure` 속성을 붙이려면 `1`로 설정합니다. |

Supabase에서 이메일 가입 확인을 켜고 **Confirm sign up** 메일 본문에 `{{ .Token }}`을 넣습니다.
로컬에서 가입 이메일을 받으려면 Supabase Auth의 발송 설정도 준비합니다.

아래 명령으로 환경 변수를 읽은 뒤 웹 서버를 실행합니다.

```sh
set -a
source ./.env
set +a
python -m src.pipelines.service.backend.web --port 8509
```

브라우저에서 [http://127.0.0.1:8509/](http://127.0.0.1:8509/)를 엽니다.
웹 서버가 프런트엔드 정적 파일과 API를 함께 제공합니다. 팀 VM에서는 할당받은 포트와 SSH 포트 포워딩을 사용합니다.

## 모델 서버 연결

서빙팀에게 **웹 백엔드에서 접근할 수 있는 모델 서버의 기본 주소**를 받아 `AI_BASE_URL`에 설정한 뒤 웹 서버를 재시작합니다.
기본 주소에 공통 경로가 있다면 포함합니다. 예를 들어 생성 요청 주소가
`http://model-server:8000/api/v1/generate`이면 `AI_BASE_URL=http://model-server:8000/api/v1`입니다.

웹 백엔드는 아래 API에 `multipart/form-data` 요청을 보냅니다.

| 경로 | 전송 항목 |
|---|---|
| `POST /generate` | 원본 JPG 또는 PNG 한 장과 매장 정보, 최초 프롬프트 |
| `POST /edit` | 수정 대상 PNG 한 장과 `commonPrompt`, `imagePrompt` 중 입력된 값. 대상이 여러 장이면 순차 호출합니다. |
| `POST /copy` | 매장 정보와 선택한 PNG 파일을 게시 순서대로 전송해 캡션과 해시태그를 받습니다. |

생성 응답의 서명 URL에서 PNG를 내려받아 결과를 Supabase Postgres에 저장합니다.
현재 단건 수정 응답은 PNG 바이너리 또는 동일 출처의 다운로드 URL을 담은 JSON을 받습니다.
실제 서빙 서버와 연결할 때는 최종 응답 형식과 파일 용량 제한을 서빙팀 명세에 맞춰 확인합니다.

## 채팅 보관 작업

웹 서버가 실행 중이면 사용자 요청과 별개로 1분마다 보관 작업을 실행합니다.
마지막 수정 시간으로부터 120시간이 지난 채팅과 연결된 파일을 함께 삭제합니다.
상시 실행되지 않는 환경에서는 인프라 예약 작업에서 아래 명령을 1분마다 실행합니다.

```sh
python -m src.pipelines.service.backend.retention
```

예약 작업에는 웹 서버와 같은 `SUPABASE_DB_URL`이 필요합니다. 만료 안내를 보내려면
서버 환경에 Resend 발송 키 `RESEND_API_KEY`와 Resend에서 사용할 수 있는 발신 주소
`ONECUT_RETENTION_FROM`을 설정하세요. 가입자가 테스트 계정 외에도 있을 때는
Resend에서 인증한 발신 도메인을 사용합니다.
