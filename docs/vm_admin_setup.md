# VM 세팅 가이드 (관리자용)

팀 VM을 처음 만들고 6명이 함께 쓸 수 있게 준비하는 순서입니다.
**VM 관리자(모델 서빙 담당 1명)가 한 번만** 따라 하면 됩니다. 걸리는 시간은 30분~1시간 정도입니다.

> 팀원이 VM에 접속하는 방법은 [VM 접속 가이드 (팀원용)](vm_member_guide.md)에 따로 있습니다.

---

## 시작 전에

### 채워 넣을 값

이 문서에서 `< >`로 표시한 부분은 팀마다 다릅니다. 진행하면서 아래 표를 채워 두면, 마지막에 팀원에게 공지할 때 그대로 쓸 수 있습니다.

| 항목 | 값 | 어디서 확인하나 |
|---|---|---|
| GCP 프로젝트 ID | `<PROJECT_ID>` | GCP 콘솔 상단의 프로젝트 선택 창 |
| region | `us-central1` (chunk4 배정 팀은 `us-east1`) | 코드잇 공지 |
| zone | `<ZONE>` (예: `us-central1-a`) | VM을 만들 때 고른 값 |
| VM 이름 | `<VM_NAME>` (예: `salon-vm`) | VM을 만들 때 정한 값 |
| 외부 IP | `<EXTERNAL_IP>` | VM을 만든 뒤 VM 목록 화면 |

### 꼭 지켜야 하는 코드잇 규칙

- **VM은 팀 프로젝트당 1대만** 만듭니다. 추가로 만들면 별도 과금됩니다.
- **디스크는 100GB 이하**로 만듭니다.
- 프로젝트가 끝날 때까지 24시간 무료로 쓸 수 있으니 **VM을 끄지 않습니다.** 껐다 켜면 외부 IP가 바뀌어서 팀원 모두가 접속 설정을 다시 해야 합니다.

### 준비물

- [ ] 코드잇에서 받은 GCP 계정(`LMS아이디@codeit-sprint.kr`)으로 로그인, 비밀번호 변경
- [ ] **2단계 인증 설정** (설정하지 않으면 일정 기간 뒤 접속이 막힙니다)
- [ ] GCP 콘솔에서 우리 팀 프로젝트가 보이는지 확인 (안 보이면 디스코드 공지의 팀별 프로젝트 링크로 이동)

---

## 1단계. VM 만들기

GCP 콘솔에서 **Compute Engine → VM 인스턴스 → 인스턴스 만들기**로 들어가 아래 값으로 설정합니다.

| 설정 | 값 | 이유 |
|---|---|---|
| 이름 | `<VM_NAME>` | 팀원들이 접속할 때 쓰는 이름 |
| 리전 | `us-central1` | 코드잇 지정 |
| 영역(zone) | L4를 고를 수 있는 곳 (예: `us-central1-a`) | zone마다 GPU 재고가 다릅니다 |
| 머신 구성 | **GPU** 탭 → GPU 유형 `NVIDIA L4`, 개수 1 → 머신 유형 `g2-standard-4` | 코드잇 제공 사양 (vCPU 4, 메모리 16GB) |
| 부팅 디스크 | **변경** → Ubuntu 22.04 계열 중 NVIDIA 드라이버가 포함된 이미지 (코드잇 예시: `ubuntu-accelerator-2204-amd64-with-nvidia-550`) | CUDA 드라이버를 직접 설치하지 않아도 됩니다 |
| 부팅 디스크 크기 | **100GB** | 코드잇 상한 |
| 방화벽 | HTTP 트래픽 허용, HTTPS 트래픽 허용 **모두 체크 해제** | 외부에서 아무나 서버에 접근하지 못하게 막습니다 |

> **"리소스가 부족합니다" 오류가 나면** 그 zone에 L4가 남아 있지 않은 것입니다. 같은 region의 다른 zone(`us-central1-b`, `us-central1-c` 등)으로 바꿔서 다시 만들어 보세요.

<details>
<summary>콘솔 대신 명령어로 만들고 싶다면 (Cloud Shell에서 실행)</summary>

```bash
gcloud compute instances create <VM_NAME> \
  --project=<PROJECT_ID> \
  --zone=<ZONE> \
  --machine-type=g2-standard-4 \
  --maintenance-policy=TERMINATE \
  --image-family=ubuntu-accelerator-2204-amd64-with-nvidia-550 \
  --image-project=ubuntu-os-accelerator-images \
  --boot-disk-size=100GB
```

`g2-standard-4`에는 L4 GPU 1장이 자동으로 붙습니다. GPU가 붙은 VM은 `--maintenance-policy=TERMINATE`가 필요합니다.

</details>

만들고 나면 VM 목록에서 **외부 IP**를 확인해 위 표에 적어 두세요.

---

## 2단계. 접속해서 동작 확인하기

VM 목록에서 VM 오른쪽의 **SSH** 버튼을 누르면 브라우저에서 터미널이 열립니다. 아래 명령을 차례로 실행합니다.

```bash
nvidia-smi                 # GPU 이름에 "NVIDIA L4"가 보이면 정상
python3 --version          # Python 3.10.x 가 나오면 정상
df -h /                    # 전체 크기가 100G 근처인지 확인
```

`nvidia-smi`가 동작하지 않으면 부팅 디스크 이미지에 드라이버가 없는 것입니다. 이미지를 다시 확인하고, 해결되지 않으면 코드잇 운영진에게 문의하세요.

> `sudo` 명령이 권한 오류로 막히면 GCP 계정에 관리자 권한이 없는 것입니다. 이 경우 코드잇 운영진에게 문의하세요.

---

## 3단계. 공용 도구 설치하기

```bash
sudo apt update
sudo apt install -y git tmux python3.10-venv acl nvtop
```

| 도구 | 쓰는 곳 |
|---|---|
| `git` | 레포 clone, 커밋 |
| `tmux` | SSH 연결이 끊겨도 오래 걸리는 작업(학습 등)이 계속 돌게 함 |
| `python3.10-venv` | 팀원 각자의 가상환경 만들기 |
| `acl` | 공용 폴더를 팀원 모두가 쓸 수 있게 권한 설정 |
| `nvtop` | GPU를 누가 얼마나 쓰고 있는지 보기 쉽게 확인 |

---

## 4단계. 팀 그룹과 공용 폴더 만들기

모델 파일은 크기가 커서(하나에 2~7GB) 각자 받으면 디스크가 금방 찹니다. 그래서 **모델 캐시와 데이터는 공용 폴더 하나에 모아** 함께 씁니다.

```bash
# 팀 그룹 만들기
sudo groupadd team

# 공용 폴더 만들기
sudo mkdir -p /opt/shared/hf-cache /opt/shared/data

# 팀 그룹이 읽고 쓸 수 있게 하기
sudo chgrp -R team /opt/shared
sudo chmod -R 2775 /opt/shared

# 나중에 누가 만든 파일이든 팀원 모두가 쓸 수 있게 하기
sudo setfacl -R -m g:team:rwX /opt/shared
sudo setfacl -R -d -m g:team:rwX /opt/shared
```

| 폴더 | 용도 |
|---|---|
| `/opt/shared/hf-cache` | HuggingFace 모델 캐시. 모델을 한 번만 받아서 모두 공유 |
| `/opt/shared/data` | 팀 공용 데이터. 넣은 뒤에는 되도록 읽기만 |

---

## 5단계. 모든 계정에 공용 설정 적용하기

누가 접속하든 모델이 공용 캐시에 저장되도록 설정합니다.

```bash
echo 'export HF_HOME=/opt/shared/hf-cache' | sudo tee /etc/profile.d/team.sh
```

다시 접속한 뒤 `echo $HF_HOME`을 실행해서 `/opt/shared/hf-cache`가 나오면 정상입니다.

---

## 6단계. 팀원 계정을 팀 그룹에 넣기

팀원의 VM 계정은 **팀원이 처음 접속할 때 자동으로** 만들어집니다. 그래서 이 단계는 팀원들이 접속 가이드의 첫 접속을 마친 뒤에 진행합니다.

```bash
# 만들어진 계정 목록 보기
ls /home

# 팀원 계정마다 실행 (본인 계정 포함)
sudo gpasswd -a <계정이름> team
```

그룹은 **다시 접속해야 적용**됩니다. 팀원에게 "VS Code 창을 닫고 다시 접속해 주세요"라고 알려주세요.

- [ ] 김민혁 (PM)
- [ ] 이재웅 (모델 개발)
- [ ] 서정철 (모델 개발)
- [ ] 김연주 (모델 서빙)
- [ ] 김시현 (모델 서빙)
- [ ] 김도영 (서비스 개발)

---

## 7단계. 팀에 공지하기

아래 내용을 **디스코드 팀 채널에만** 올립니다.

> 🔒 **외부 IP와 계정 이름은 GitHub README 등 공개된 곳에 적지 않습니다.** 공개 레포에 올라가면 누구나 접속을 시도할 수 있습니다.

```
[팀 VM 준비 완료]
- VM 이름: <VM_NAME>
- zone: <ZONE>
- 프로젝트 ID: <PROJECT_ID>
- 외부 IP: <EXTERNAL_IP>
접속 방법은 "VM 접속 가이드 (팀원용)"을 따라 해 주세요.
첫 접속을 마치면 이 메시지에 ✅ 남겨 주세요. 팀 그룹에 추가해 드립니다.
```

---

## 운영하면서 할 일

처음 세팅이 끝나면 관리자 일은 많지 않습니다.

### 매주 월요일: 디스크 확인

```bash
df -h /                          # 전체 사용량
du -sh /opt/shared/hf-cache      # 모델 캐시 크기
sudo du -sh /home/*              # 팀원별 사용량
```

결과를 디스코드에 공유하고, **사용량이 80GB를 넘으면** 쓰지 않는 모델과 생성 결과물 정리를 요청합니다.

**디스크 예산 (참고)**

| 용도 | 예산 |
|---|---|
| OS와 드라이버 | 약 20GB |
| GPU를 쓰는 4명의 가상환경 (1인당 약 6GB) | 약 24GB |
| 공용 모델 캐시 | 약 30GB |
| 데이터, 생성 결과 | 약 10GB |
| 여유분 | 약 16GB |

### GPU가 계속 점유되어 있을 때

```bash
nvidia-smi                       # 맨 아래에 GPU를 쓰는 프로세스 번호(PID)가 나옴
ps -o user=,cmd= -p <PID>        # 그 프로세스를 누가 실행했는지 확인
```

주인을 찾아서 종료를 요청합니다. 대부분 **켜 둔 채로 잊은 노트북 커널**입니다.

### 팀원의 설치 요청 처리

팀원은 `sudo`를 쓰지 않기로 했습니다. `apt`로 설치해야 하는 프로그램이 있으면 관리자에게 요청하니, 확인하고 설치해 주세요. Python 패키지는 각자의 가상환경에 설치하면 되니 관리자가 처리할 필요가 없습니다.

### VM이 꺼져 있을 때

GPU VM은 구글의 유지보수 때문에 드물게 꺼질 수 있습니다. 그럴 때는

1. GCP 콘솔에서 VM을 **시작**합니다.
2. **외부 IP가 바뀌었는지** 확인합니다.
3. 바뀌었다면 디스코드에 새 IP를 공지합니다. 팀원은 SSH 설정의 `HostName`만 바꾸면 됩니다.

---

## 세팅 완료 체크리스트

- [ ] VM 1대 생성 (L4 GPU, g2-standard-4, 디스크 100GB, HTTP/HTTPS 허용 해제)
- [ ] `nvidia-smi`, Python 3.10 확인
- [ ] 공용 도구 설치
- [ ] `team` 그룹, `/opt/shared` 폴더와 권한 설정
- [ ] `HF_HOME` 공용 설정
- [ ] 디스코드 팀 채널에 접속 정보 공지
- [ ] 팀원 6명 모두 팀 그룹에 추가
- [ ] 백업 관리자(서빙 담당 다른 1명)에게 이 문서 공유
