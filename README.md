# 네일샷 AI · 네일 장면 사진 만들기

네일 팁 사진 1장을 올리면, AI가 사진 속 **팁 세트(한 줄에 5개)** 를 하나씩 찾아냅니다.
세트마다 `nailhand` 폴더의 손 장면 사진(와인, 머그컵, 칵테일, 향수 등) 중 어울리는 사진을 골라
**그 세트를 손톱에 입히고, 배경과 음료 색만 세트에 어울리게 바꿔** 줍니다.
상호명이나 배너 문구는 넣지 않습니다.

```
팁 사진 1장 ─▶ ⓪ 세트 나누기 (gpt-6-luna)
               ─▶ 세트마다 ① 팁 인식 + 장면 사진 고르기 (gpt-6-luna)
                           ② 손톱에 세트 입히기 + 배경·음료 색 바꾸기 (gpt-image-2)
               ─▶ 세트 수만큼 완성 사진 (최대 12장)
```

- 그래픽카드가 필요 없습니다. 모든 AI 처리는 OpenAI API로 합니다.
- 브라우저(크롬 권장)에서 `http://localhost:8000` 으로 사용합니다.

---

## 1. 준비물

| 항목 | 내용 |
|---|---|
| Python | **3.11 이상** (3.14 에서 확인). Windows는 [python.org](https://www.python.org/downloads/) 에서 설치할 때 **"Add python.exe to PATH"** 를 꼭 체크하세요. |
| Git | [git-scm.com](https://git-scm.com/downloads) |
| OpenAI API 키 | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) 에서 발급합니다. 결제 수단이 등록돼 있어야 합니다. |

---

## 2. 내려받기

```bash
git clone https://github.com/<계정>/<저장소>.git
cd <저장소>
```

이미 받아 둔 PC에서 최신 코드로 바꿀 때는 `git pull` 만 하면 됩니다.

---

## 3. API 키 넣기

`.env.example` 을 복사해서 이름을 `.env` 로 바꾸고, 키를 넣습니다.

```bash
# Windows (명령 프롬프트)
copy .env.example .env
# Mac / Linux
cp .env.example .env
```

`.env` 내용:

```
OPENAI_API_KEY=sk-...내 키...
```

> `.env` 는 `.gitignore` 에 들어 있어서 깃허브에 올라가지 않습니다. 키를 코드나 다른 파일에 적지 마세요.

---

## 4. 실행

### Windows — 더블클릭

`start_server.bat` 을 더블클릭합니다.

- **처음 한 번**은 `venv_api` 폴더(파이썬 환경)를 만들고 필요한 패키지를 설치합니다. 몇 분 걸립니다.
- 창에 `웹 화면: http://localhost:8000` 이 나오면 크롬에서 그 주소를 엽니다.
- 서버를 끄려면 그 검은 창을 닫습니다.

### Mac / Linux — 터미널

```bash
python3 -m venv venv_api
source venv_api/bin/activate
pip install -r requirements.txt
python nail_server.py
```

다음부터는 `source venv_api/bin/activate` 후 `python nail_server.py` 만 하면 됩니다.

### API 없이 화면만 확인 (요금 없음)

```bash
start_server.bat --fake          # Windows
python nail_server.py --fake     # Mac / Linux
```

AI 대신 가짜 결과를 돌려줍니다. 화면이 잘 뜨는지만 확인할 때 쓰세요.

---

## 5. 사용법

1. **네일 팁 사진 1장**을 올립니다. 끌어다 놓기나 붙여넣기(Ctrl+V)도 됩니다. JPG·PNG·WebP, 10MB 이하.
2. **'팁 모음 사진 · 세트 나누기'** 가 켜져 있는지 확인합니다. 기본으로 켜져 있습니다.
3. 품질(빠르게 / 기본 / 고화질)을 고르고 **세트 나눠서 장면 사진 만들기** 를 누릅니다.
4. 세트마다 카드가 생기고, 완성되면 사진이 나옵니다.
   - **한꺼번에 미리보기**: 완성 사진을 넘기며 확대·축소해 봅니다.
   - **전체 다운로드 (ZIP)**: 완성 사진과 `sets/` 폴더(세트별로 잘린 팁 이미지)를 한 번에 받습니다.

### 잘 나오는 사진

- 팁이 **한 줄에 5개씩** 반듯하게 놓인 사진이 가장 정확합니다. 2열 × 6행 같은 진열판 사진이 좋습니다.
- 사진 가장자리에서 잘린 세트는 뺍니다.
- 투명한 팁이 많다면, 흰 배경보다 **무광의 중간 회색 단색 배경**에서 찍은 사진이 더 잘 인식됩니다.
- 대각선으로 겹쳐 놓은 포스터형 사진도 나눌 수 있지만, 한 줄의 개수가 5개가 아니면 세트가 부정확할 수 있습니다.

---

## 6. 다른 기기(휴대폰 등)에서 접속하기

같은 와이파이(공유기)에 연결된 기기에서 쓰려면 `settings.yaml` 을 이렇게 바꾸거나:

```yaml
server:
  lan: true
```

`start_server.bat --lan` 으로 실행합니다. 서버 창에 나오는 `다른 기기에서: http://192.168.x.x:8000` 주소로 접속하면 됩니다.

- Windows 방화벽에서 "액세스 허용"을 묻는 창이 뜨면 **개인 네트워크**를 허용하세요.
- 손님에게 공개할 때는 `settings.yaml` 에서 `server.api_docs: false`, `server.hide_cost: true` 를 권장합니다.
- 인터넷 전체에 공개하는 용도로는 만들어지지 않았습니다. 로그인 기능이 없어서, 주소를 아는 누구나 내 API 키로 요금을 쓸 수 있습니다.

---

## 7. 설정 (`settings.yaml`)

자주 바꾸는 값:

| 항목 | 기본값 | 설명 |
|---|---|---|
| `openai.model` | `gpt-image-2` | 이미지 모델 |
| `design_analysis.model` | `gpt-6-luna` | 인식 모델 (세트 나누기 · 팁 인식 · 장면 고르기) |
| `server.port` | `8000` | 웹 주소 포트 |
| `server.lan` | `false` | 같은 와이파이의 다른 기기 접속 허용 |
| `banner.max_designs` | `1` | 한 번에 올릴 수 있는 사진 수 |
| `banner.apply_mode` | `scene` | 장면 사진 방식 (이 화면은 항상 scene 으로 보냅니다) |
| `sheet.max_sets` | `12` | 사진 1장에서 만드는 최대 장수. 넘는 세트는 뺍니다 (비용 보호). |
| `sheet.skip_incomplete` | `true` | 가장자리에서 잘린 세트는 뺍니다. |
| `debug.save_images` | `true` | `outputs_api/` 에 보낸 사진·프롬프트·결과를 저장합니다. 용량이 계속 늘어나니 필요 없으면 `false` 로 바꾸세요. |

가격이 바뀌면 `prices.json` 의 숫자만 고치면 됩니다.

---

## 8. 장면 사진 늘리기 (`nailhand/`)

```
nailhand/
├─ 레드와인(빨간색,섹시)/        ← 폴더 이름 = 분위기(키워드,키워드). AI가 고르는 기준
│   ├─ 블랙체리 와인빛 글리터 네일-1.png
│   └─ ...
├─ 머그컵(가을,겨울)/
└─ hand_side.json               ← 사진마다 왼손(left) / 오른손(right) / 양손(both)
```

- 새 분위기는 `이름(키워드,키워드)` 형식으로 폴더를 만들고 사진을 넣으면 됩니다.
- 새 사진은 서버를 켤 때 왼손/오른손을 한 번 자동으로 확인해 `hand_side.json` 에 저장합니다. 틀렸으면 그 파일에서 직접 고치세요.
- 손가락 순서 규칙: 한 줄의 팁을 왼쪽부터 1~5번이라 하면 **1번=엄지, 2번=검지, 3번=중지, 4번=약지, 5번=새끼** 입니다 (왼손·오른손 같음).

---

## 9. 비용과 기록

- 서버 창과 웹 화면에 요청마다 비용이 나옵니다. 화면 위쪽에는 오늘 누적과 전체 누적이 나옵니다.
- 기록은 `usage_log.csv` 에 쌓입니다. 엑셀로 열 수 있습니다. PC마다 따로 쌓이고 깃허브에는 올라가지 않습니다.
- 참고: 완성 사진 1장에 약 80원(기본 품질)이 듭니다. 세트가 12개인 사진이면 약 1,000원입니다.

---

## 10. 폴더 구성

| 경로 | 설명 |
|---|---|
| `nail_server.py` | 서버 본체 (FastAPI) |
| `api_schema.py` | API 입력·출력 형식 |
| `web/banner.html` | 웹 화면 |
| `nailhand/` | 손 장면 사진 (필수) |
| `settings.yaml` | 설정 |
| `prices.json` | 모델 가격표 (비용 계산용) |
| `requirements.txt` | 필요한 파이썬 패키지 |
| `start_server.bat` | Windows 실행 파일 |
| `.env.example` | API 키 파일 예시 → `.env` 로 복사해서 사용 |
| `API_SPEC.md` | API 명세. 서버를 켜고 `http://localhost:8000/docs` 에서도 볼 수 있습니다. |
| `.vscode/` | VS Code 에서 F5로 실행·디버그할 때 쓰는 설정 |

깃허브에 올라가지 않아서 PC마다 새로 생기는 것: `.env`, `venv_api/`, `outputs_api/`, `usage_log.csv`, `__pycache__/`

---

## 11. 문제 해결

| 증상 | 해결 |
|---|---|
| `[ERROR] Python 3 was not found` | Python을 설치하고(PATH 체크) `start_server.bat` 을 다시 실행하세요. |
| `OPENAI_API_KEY 가 없어요` | `.env` 파일이 있는지, 키가 맞는지 확인하세요 (3번). |
| `서버에 연결할 수 없어요` (웹 화면) | 서버 창이 켜져 있는지 확인하세요. |
| 패키지 설치가 실패했어요 | 인터넷 연결을 확인하고, `venv_api` 폴더를 지운 뒤 다시 실행하세요. |
| 화면이 예전 모습 그대로예요 | 브라우저에서 **Ctrl + Shift + R** 로 새로고침하세요. |
| 세트를 못 나누고 사진 전체로 1장만 나와요 | 팁이 한 줄에 4~6개씩 보이는 사진인지 확인하세요. 서버 창의 `세트 나누기 … N세트` 줄에서 찾은 개수를 볼 수 있습니다. |
| `OpenAI 사용량/요금 한도` | OpenAI 결제 한도와 잔액을 확인하세요. |
| `usage_log.csv 가 엑셀에서 열려 있어…` | 엑셀을 닫으세요. 열려 있으면 기록을 쓰지 못합니다. |
| 포트 8000 이 이미 사용 중 | `settings.yaml` 의 `server.port` 를 바꾸거나 `start_server.bat --port 8001` 로 실행하세요. |
