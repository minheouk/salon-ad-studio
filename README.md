# salon-ad-studio

헤어·네일 사업자가 디자인 도구 없이도 **가게 광고 이미지를 직접 만들 수 있게** 돕는 생성형 AI 서비스입니다.

> 코드잇 스프린트 AI 고급 프로젝트 (2026.11.19 ~ 2026.12.18)

## 목차

- [프로젝트 소개](#프로젝트-소개)
- [팀원](#팀원)
- [일정](#일정)
- [빠른 시작](#빠른-시작)
- [폴더 구조](#폴더-구조)
- [협업 규칙](#협업-규칙)
- [동작 방식](#동작-방식)
- [결과](#결과)
- [보고서와 협업일지](#보고서와-협업일지)

## 프로젝트 소개

| 항목 | 내용 |
|---|---|
| 사용자 | 헤어숍, 네일숍을 운영하는 소상공인 |
| 해결하려는 문제 | 디자인 역량이나 예산이 부족해 온라인 광고 콘텐츠를 만들기 어려움 |
| 서비스 기능 | 광고 제작, 광고에 쓸 이미지 생성 |
| 결과물 | 사업체 광고 이미지 |

## 팀원

| 이름 | 역할 | 담당 | GitHub |
|---|---|---|---|
| 김민혁 | PM | 기획, 일정·협업 관리, 서비스 테스트와 평가 | [@minheouk](https://github.com/minheouk) |
| 이재웅 | 모델 개발 | 생성 모델 선정, 생성 파이프라인, 성능 평가 | [@mitguchin](https://github.com/mitguchin) |
| 서정철 | 모델 개발 | 생성 모델 선정, 생성 파이프라인, 성능 평가 | [@SeoJungCheol](https://github.com/SeoJungCheol) |
| 김연주 | 모델 서빙 | 서빙 전략, 추론 속도·메모리 최적화 | [@aiengineeung-del](https://github.com/aiengineeung-del) |
| 김시현 | 모델 서빙 | 서빙 전략, 추론 속도·메모리 최적화 | [@dypower1559-cell](https://github.com/dypower1559-cell) |
| 김도영 | 서비스 개발 | 화면 구현, 백엔드 로직 | [@diokim17](https://github.com/diokim17) |

## 일정

| 시작 | 종료 |
|---|---|
| 2026.11.19 (목) | 2026.12.18 (금) |

## 빠른 시작

Python 3.10과 NVIDIA GPU 기준입니다. PyTorch를 먼저 CUDA 빌드로 설치한 뒤 `requirements.txt`를 설치합니다.

```bash
git clone https://github.com/minheouk/salon-ad-studio.git
cd salon-ad-studio
python3.10 -m venv .venv
source .venv/bin/activate                    # Windows: .venv\Scripts\activate
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
python -c "import torch; print(torch.cuda.is_available())"   # True 가 나오면 정상
```

`.env.example`을 복사해서 `.env`를 만들고 OpenAI 키를 넣습니다. 키는 디스코드 팀 채널에서 받습니다.

```bash
cp .env.example .env
```

서비스 실행 방법은 개발이 진행되면 추가합니다.

### 팀 VM에서 작업하기

팀 VM(NVIDIA L4)에 접속하는 방법과 함께 쓰는 규칙은 [VM 접속 가이드](docs/vm_member_guide.md)에 있습니다.
VM 관리자는 [VM 세팅 가이드](docs/vm_admin_setup.md)를 봅니다.

> VM의 IP 주소와 계정 이름은 이 공개 레포에 적지 않습니다. 디스코드 팀 채널에서 확인하세요.

## 폴더 구조

```
salon-ad-studio/
├── README.md                이 문서
├── requirements.txt         직접 쓰는 패키지만
├── configs/                 모델·프롬프트·실험 설정
├── assets/fonts/            광고에 쓰는 한글 폰트 (상업적 사용 가능한 것만)
├── eval/                    고정 평가 입력 세트
├── experiments/             실험 기록 (결과 요약과 비교 이미지)
├── notebooks/               실험용 노트북
├── scripts/                 실행 스크립트
├── docs/                    📋 PM: 기획, 회의록, 평가, 보고서, 의사결정, 협업일지 링크, VM 가이드
│   └── agent_rules/         AI 코딩 도구가 지켜야 할 팀 공통 규칙과 변경 내역
├── src/
│   ├── common/              🔒 공용 계약 (역할 사이에 주고받는 데이터 형식, 설정). 변경 시 PM 승인
│   └── pipelines/
│       ├── model/           🎨 모델 개발: 이미지 생성, 광고 문구, 파인튜닝, 평가, 문구·로고 합성
│       ├── serving/         ⚡ 모델 서빙: 추론 API, 최적화, 목업 API
│       └── service/         🖥️ 서비스 개발: 화면, 백엔드
└── tests/                   역할별 테스트
```

`data/`, `outputs/`, `logs/`, `.env`는 커밋하지 않습니다.

## 협업 규칙

자세한 규칙은 [팀 공통 규칙](docs/agent_rules/base_rules.md)에 있습니다.

`AGENTS.md`(Codex)와 `CLAUDE.md`(Claude Code)는 각자 로컬에서 관리하며 커밋하지 않습니다.
clone한 뒤 [AI 코딩 도구 규칙 안내](docs/agent_rules/README.md)를 따라 공통 규칙을 복사해서 쓰세요.

### 브랜치

```
main   ← 제출·발표용. PM만 merge (dev에서만)
 └ dev ← 통합 테스트 (기본 브랜치)
    ├ feat/<역할>-<작업>   기능 개발 → dev로 PR
    ├ fix/<역할>-<작업>    버그 수정 → dev로 PR
    ├ docs/<주제>          문서 → dev로 PR
    └ exp/<역할>-<주제>    자유 실험 (merge 안 해도 됨, 결과는 experiments/에 기록)
```

`<역할>`은 `model`, `serving`, `service`, `pm` 중 하나입니다.

### PR 리뷰와 merge

| PR | 승인 | merge |
|---|---|---|
| 모델 개발, 모델 서빙 작업 | 같은 역할 팀원 1명 | 작성자 |
| 서비스 개발 작업 | PM | 작성자 |
| PM의 문서 작업 | 팀원 누구나 1명 | PM |
| "PM 확인 필요"가 체크된 PR | PM | PM |
| `dev` → `main` | — | PM만 |

- **본인 작업은 본인 GitHub 계정으로 PR**을 올립니다. PR과 커밋 기록이 개인 기여의 근거가 됩니다.
- 리뷰어는 세 가지만 확인합니다. **돌아가는가, 들어가면 안 되는 것(키·데이터·결과물)이 없는가, 자기 역할의 폴더 안에서 바꿨는가.**
- 리뷰 요청을 받으면 반나절 안에 확인해 주세요.

### 커밋

- 메시지는 접두사 + 한국어 요약입니다. 예: `feat: 배경 이미지 생성 파이프라인 추가`
- 접두사: `feat:` `fix:` `docs:` `exp:` `chore:`
- 처음 커밋하기 전에 `git config user.name`과 `user.email`이 본인 것인지 확인합니다.

### 커밋하기 전에

```bash
ruff check .                  # 함수 복잡도 검사
python -m pytest -q tests     # 테스트
```

- 노트북은 출력을 지우고 커밋합니다.
- API 키, 데이터, 생성 결과물, 모델 가중치는 커밋하지 않습니다. 이 레포는 공개 레포입니다.

## 동작 방식

> 개발이 진행되면 서비스 흐름도와 사용한 모델·기술을 추가합니다.

## 결과

> 생성 예시 이미지, 평가 결과, 시연 영상을 추가합니다.

## 보고서와 협업일지

| 항목 | 링크 |
|---|---|
| 최종 보고서 (PDF) | 추가 예정 |
| 팀원별 협업일지 | [docs/journals.md](docs/journals.md) |
