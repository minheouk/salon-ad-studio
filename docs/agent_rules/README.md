# AI 코딩 도구 규칙

`AGENTS.md`(Codex)와 `CLAUDE.md`(Claude Code)는 **각자 자기 PC와 VM에서 따로 관리**합니다.
사람마다 쓰는 도구와 설정이 달라서, 이 파일들을 레포에 함께 올리면 서로의 내용이 덮어써지는 문제가 생기기 때문입니다.
그래서 두 파일은 `.gitignore`에 들어 있어 커밋되지 않습니다.

대신 **팀 공통 규칙은 이 폴더에서** 관리합니다.

| 파일 | 내용 |
|---|---|
| [base_rules.md](base_rules.md) | 팀 공통 규칙 원본 (AI가 읽기 좋게 영어로 작성) |
| [changelog.md](changelog.md) | 공통 규칙에 추가·변경된 내역 |

## 처음 설정하기

레포를 clone한 뒤, 쓰는 도구에 맞게 공통 규칙을 복사합니다.

```bash
cp docs/agent_rules/base_rules.md AGENTS.md     # Codex를 쓰는 경우
cp docs/agent_rules/base_rules.md CLAUDE.md     # Claude Code를 쓰는 경우
```

복사한 파일 아래쪽에 **나만의 규칙을 자유롭게 덧붙여도** 됩니다. 이 파일은 커밋되지 않습니다.

## 공통 규칙이 바뀌었을 때

1. 팀원 누구나 바꾸자고 제안할 수 있습니다. 디스코드에서 먼저 이야기해 주세요.
2. `base_rules.md`를 고치고 `changelog.md`에 내역을 한 줄 추가해서 PR을 올립니다.
3. 공용 파일이라 **PM이 승인하고 merge**합니다.
4. merge되면 각자 `changelog.md`를 보고 내 로컬 `AGENTS.md` / `CLAUDE.md`에 바뀐 부분을 반영합니다.
