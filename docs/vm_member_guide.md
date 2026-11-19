# VM 접속 가이드 (팀원용)

팀 VM(NVIDIA L4 GPU)에 접속해서 작업을 시작하는 방법과, 6명이 함께 쓰기 위한 규칙입니다.
**처음 한 번은 1~4단계를 순서대로** 따라 하고, 그 뒤로는 [매일 작업할 때](#매일-작업할-때)와 [함께 쓰는 규칙](#함께-쓰는-규칙)만 보면 됩니다.

> VM의 외부 IP, VM 이름, zone, 프로젝트 ID는 **디스코드 팀 채널의 VM 관리자 공지**에서 확인하세요.
> 공개된 곳에 올리지 않기 위해 이 문서에는 적지 않았습니다.

---

## 처음 한 번만

### 1단계. 내 PC 준비하기

- [ ] 코드잇에서 받은 GCP 계정(`LMS아이디@codeit-sprint.kr`)으로 로그인, 비밀번호 변경
- [ ] **2단계 인증 설정** (설정하지 않으면 일정 기간 뒤 접속이 막힙니다)
- [ ] [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) 설치
- [ ] [VS Code](https://code.visualstudio.com/) 설치, 확장 프로그램 **Remote - SSH** 설치

### 2단계. 처음 접속하기 (내 VM 계정 만들기)

내 PC의 터미널(Windows는 PowerShell)에서 실행합니다.

```bash
gcloud auth login                              # 브라우저가 열리면 codeit-sprint.kr 계정으로 로그인
gcloud config set project <PROJECT_ID>
gcloud compute ssh <VM_NAME> --zone <ZONE>
```

- 처음 실행하면 SSH 키를 만들지 물어봅니다. 그대로 Enter를 누르면 됩니다.
- 접속에 성공하면 **VM에 내 계정이 자동으로 만들어집니다.**

접속된 VM 터미널에서 내 계정 이름을 확인해 둡니다. 3단계에서 씁니다.

```bash
whoami
```

확인했으면 `exit`로 나옵니다. 그리고 **디스코드 공지에 ✅를 남겨 주세요.** VM 관리자가 내 계정을 팀 그룹에 넣어 줘야 공용 폴더를 쓸 수 있습니다.

### 3단계. VS Code로 접속하기

내 PC의 SSH 설정 파일을 열어 아래 내용을 추가합니다.

- Windows: `C:\Users\<내 PC 사용자>\.ssh\config`
- macOS: `~/.ssh/config`

파일이 없으면 새로 만들면 됩니다.

```
Host salon-vm
    HostName <EXTERNAL_IP>
    User <2단계에서 확인한 whoami 결과>
    IdentityFile ~/.ssh/google_compute_engine
    IdentitiesOnly yes
    ServerAliveInterval 30
```

VS Code에서 `F1` → **Remote-SSH: Connect to Host...** → `salon-vm`을 고릅니다.
왼쪽 아래에 `SSH: salon-vm`이 보이면 접속된 것입니다.

### 4단계. VM에서 작업 환경 만들기

VS Code의 터미널(`` Ctrl+` ``)에서 실행합니다. 이 터미널은 VM 안에서 돌아갑니다.

**① git 사용자 정보 설정 (필수)**

```bash
git config --global user.name "<GitHub 이름>"
git config --global user.email "<GitHub에 등록한 이메일>"
```

> 지난 프로젝트에서는 이 설정을 빠뜨려서 커밋 53개가 `local-user`라는 이름으로 남았습니다.
> 누가 작업했는지 알 수 없으면 **개인 평가에서 내 기여를 증명하기 어렵습니다.** 꼭 설정하세요.

**② 레포 clone (반드시 내 홈 폴더에)**

```bash
cd ~
git clone https://github.com/minheouk/salon-ad-studio.git
cd salon-ad-studio
```

VS Code 터미널에서 `git push`를 하면 내 PC의 GitHub 로그인 정보가 그대로 쓰입니다.

AI 코딩 도구를 쓴다면 팀 공통 규칙을 내 로컬 규칙 파일로 복사합니다. 이 파일은 커밋되지 않습니다. ([안내](agent_rules/README.md))

```bash
cp docs/agent_rules/base_rules.md AGENTS.md     # Codex
cp docs/agent_rules/base_rules.md CLAUDE.md     # Claude Code
```

**③ 가상환경 만들기**

GPU를 쓰는 역할(**모델 개발, 모델 서빙**)만 VM에 가상환경을 만듭니다. 가상환경 하나가 6GB 정도라서 모두 만들면 디스크가 부족합니다.
서비스 개발과 PM은 내 PC에서 작업하는 걸 권장합니다.

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import torch; print(torch.cuda.is_available())"   # True 가 나오면 정상
```

`False`가 나오면 VM 관리자에게 알려 주세요.

**④ API 키 넣기**

`.env.example`을 복사해서 `.env`를 만들고 키를 넣습니다. 키는 디스코드 팀 채널에서 받습니다.

```bash
cp .env.example .env
nano .env               # OPENAI_API_KEY= 뒤에 키 붙여넣기, Ctrl+O 저장, Ctrl+X 종료
chmod 600 .env          # 나만 읽을 수 있게 (VM은 여럿이 같이 씁니다)
```

**⑤ 공용 설정 확인**

```bash
echo $HF_HOME           # /opt/shared/hf-cache 가 나오면 정상
groups                  # 목록에 team 이 있으면 정상
```

`team`이 없으면 관리자가 아직 그룹에 넣지 않았거나, 다시 접속하지 않은 것입니다. VS Code 창을 닫고 다시 접속해 보세요.

---

## 매일 작업할 때

1. VS Code에서 `salon-vm`에 접속
2. 내 레포 폴더(`~/salon-ad-studio`)를 열기
3. 최신 내용 받기: `git switch dev && git pull`
4. 작업 브랜치 만들기: `git switch -c feat/<역할>-<작업>`
5. GPU를 쓸 거라면 [GPU 사용 규칙](#1-gpu-공지하고-확인하고-다-쓰면-끄기) 확인

---

## 함께 쓰는 규칙

VM 한 대를 6명이 같이 씁니다. 자원이 넉넉하지 않아서, 한 명이 실수하면 모두의 작업이 멈출 수 있습니다.

| 자원 | 크기 | 주의할 점 |
|---|---|---|
| GPU (L4) | 24GB | 이미지 생성 모델 1~2개만 올려도 꽉 찹니다 |
| 메모리 | 16GB | VS Code 접속만으로도 1명당 0.5~1GB를 씁니다 |
| 디스크 | 100GB | 모델 하나가 2~7GB입니다 |

### 1. GPU: 공지하고, 확인하고, 다 쓰면 끄기

1. **쓰기 전에 확인**: `nvidia-smi`로 다른 사람이 쓰고 있는지 봅니다.
2. **디스코드 `#gpu` 채널에 공지**:
   - 시작할 때: `🟢 사용 시작 / 이름 / 작업 내용 / 예상 시간`
   - 끝났을 때: `⚪ 사용 종료`
3. **다 쓰면 반드시 종료**: 프로세스와 **노트북 커널**을 꺼 주세요. 노트북 커널은 아무것도 하지 않아도 켜져 있는 동안 GPU 메모리를 계속 차지합니다. 가장 자주 생기는 문제입니다.
4. **오래 걸리는 작업(LoRA 학습 등)은 밤 시간대에** 돌립니다. 낮에는 개발과 테스트에 씁니다.

> 처음 1~2주는 이 공지 방식으로 운영하고, 충돌이 잦으면 역할별로 시간대를 나누는 방식으로 바꿉니다.

**오래 걸리는 작업은 `tmux` 안에서 실행하세요.** 그래야 SSH 연결이 끊기거나 노트북을 덮어도 작업이 계속 돌아갑니다.

```bash
tmux new -s train        # 새 작업 공간 만들기 (이름: train)
# ... 여기서 학습 실행 ...
# Ctrl+B 를 누른 뒤 D  → 작업은 둔 채로 빠져나오기
tmux attach -t train     # 다시 들어가서 진행 상황 보기
```

### 2. 디스크: 모델은 공용 캐시에만

- 모델은 **공용 캐시(`/opt/shared/hf-cache`)에만** 받습니다. `HF_HOME`이 설정되어 있어서 HuggingFace 라이브러리를 쓰면 자동으로 이 폴더에 저장됩니다.
- 다른 사람이 이미 받은 모델은 다시 받지 않고 그대로 씁니다.
- 생성 결과물(`outputs/`)은 필요한 것만 남기고 주기적으로 지웁니다.
- 매주 월요일에 VM 관리자가 디스크 사용량을 공유합니다. 정리 요청이 오면 협조해 주세요.

### 3. 포트: 내 번호만 쓰기

여러 명이 서버를 띄우면 포트가 겹칩니다. 자기 번호만 씁니다.

| 사람 | API 서버 | 화면 (Streamlit 등) |
|---|---|---|
| 김민혁 | 8001 | 8501 |
| 이재웅 | 8002 | 8502 |
| 서정철 | 8003 | 8503 |
| 김연주 | 8004 | 8504 |
| 김시현 | 8005 | 8505 |
| 김도영 | 8006 | 8506 |
| **팀 공용 데모** | **8000** | **8500** |

- VM에서 띄운 서버는 VS Code가 **자동으로 내 PC에 연결(포트 포워딩)** 해 줍니다. 내 PC 브라우저에서 `http://localhost:<포트>`로 열면 됩니다.
- 공용 데모 포트(8000, 8500)는 시연과 통합 테스트 때만 씁니다. 쓰기 전에 디스코드에 알려 주세요.

### 4. 보안

- **내 계정으로만 접속**합니다. 계정을 같이 쓰면 누가 무엇을 했는지 기록이 섞입니다.
- `.env`는 `chmod 600`으로 나만 읽게 둡니다.
- 🔒 **GCP 방화벽에서 포트를 외부에 열지 않습니다.** 열면 외부의 누구나 우리 API를 호출해서 OpenAI 비용(팀 한도 $30)을 써 버릴 수 있습니다.
- 🔒 **VM의 IP, 계정 이름, API 키를 GitHub에 올리지 않습니다.** 우리 레포는 공개 레포입니다.
- `sudo`는 쓰지 않습니다. `apt`로 설치할 것이 있으면 VM 관리자에게 요청하세요. Python 패키지는 내 가상환경에 직접 설치하면 됩니다.

---

## 문제가 생겼을 때

| 증상 | 원인과 해결 |
|---|---|
| 접속이 안 되고 시간 초과가 남 | VM이 꺼졌거나 외부 IP가 바뀌었을 수 있습니다. 디스코드 공지를 확인하고, 새 IP가 있으면 SSH 설정의 `HostName`만 바꾸세요. |
| `CUDA out of memory` 오류 | 다른 사람이 GPU를 쓰고 있습니다. `nvidia-smi`로 확인하고 `#gpu` 채널을 보세요. 내 노트북 커널이 켜져 있는지도 확인하세요. |
| `/opt/shared`에서 `Permission denied` | 팀 그룹에 아직 안 들어갔거나 다시 접속하지 않은 것입니다. `groups`에 `team`이 있는지 확인하세요. |
| `No space left on device` | 디스크가 가득 찼습니다. 내 `outputs/`와 불필요한 파일을 지우고 VM 관리자에게 알려 주세요. |
| `torch.cuda.is_available()`이 `False` | 가상환경에 CPU 버전 torch가 설치된 경우가 많습니다. 4단계 ③을 다시 확인하고, 그래도 안 되면 VM 관리자에게 알려 주세요. |

그 밖의 문제는 **VM 관리자(모델 서빙 담당)** 에게 디스코드로 물어보세요.
