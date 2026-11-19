# Repository rules

These rules are for coding agents (Claude Code, Codex) working in this repository.
`CLAUDE.md` imports this file. Edit the rules here only.

Section 1 outranks everything else in this file.
It exists because an earlier team repository had to be abandoned once its code grew too complex to work in.

## 1. Priority rules

### 1.1 Build only what was asked

Do not write any of the following unless the person asked for it.

| Forbidden | Example |
|---|---|
| Unrequested features | An extra option, a "nice to have" setting, a second output format |
| Speculative error handling and edge-case branches | `try/except` around code that has never failed, a branch for an input nobody sends |
| Useless verification | A test that guards no behaviour, or one that re-checks what another test already checks |
| Compatibility shims | A module kept only so an old import path still works |
| Config options nobody changes | A config key or function parameter that always holds the same value |

Tests themselves are allowed. A test belongs when it guards behaviour that can actually break.

### 1.2 Write code a person can read easily

- Prefer plain, obvious code over clever code.
- Let names and structure explain the code. Comments carry only the why the code cannot show.
- One function does one thing.

### 1.3 No complex functions

Complexity is measured by ruff, configured in `pyproject.toml`.

| Rule | Limit |
|---|---|
| `C901` cyclomatic complexity | 8 per function |
| `PLR0915` statement count | 50 per function |

- When a function fails, split it or simplify it. Never silence it with `noqa` or by raising a limit.

### 1.4 Never commit these

This repository is public.

| Never commit | Keep it in |
|---|---|
| API keys, tokens, `.env`, SSH keys | `.env` (ignored) |
| Customer or salon photos, collected or downloaded images, datasets | `data/` (ignored) or `/opt/shared/data` on the team VM |
| Generated images and other model outputs | `outputs/` (ignored) |
| Model weights, LoRA files, checkpoints | `/opt/shared/hf-cache` or the Hugging Face Hub |
| Logs | `logs/` (ignored) |
| The team VM's IP address, account names, GCP project ID | Team Discord channel only |

Allowed: source code, configuration, aggregate measurements (quality scores, latency, GPU memory),
and a few small sample images in `experiments/` or `docs/` that show no real customer, person or brand.
Ignore rules do not remove already tracked files; flag any tracked file of the kinds above before a push.

## 2. Before you start

1. Read this file and the `README.md` of the folder you will work in.
2. Check `git status`, the current branch, and how far it is behind `origin/dev`.
3. Report your plan and the files you will change, then wait for the person to agree.
4. Never overwrite, revert or discard changes you did not make.

## 3. Ownership

| Role | Owns | Members (GitHub) |
|---|---|---|
| PM | `docs/` and the shared files below | minheouk |
| Model development | `src/pipelines/model/`, `tests/model/` | mitguchin, SeoJungCheol |
| Model serving | `src/pipelines/serving/`, `tests/serving/` | aiengineeung-del, dypower1559-cell |
| Service development | `src/pipelines/service/`, `tests/service/` | diokim17 |

Shared files, changed only with PM approval:
`src/common/`, `configs/`, `requirements.txt`, `pyproject.toml`, `AGENTS.md`, `CLAUDE.md`, `.github/`, `.gitignore`.

`experiments/`, `eval/` and `notebooks/` are open to everyone; change only your own files there.

Stop and report to the person, without making the change, when the task needs you to:

- change a shared file,
- change another role's folder, or
- guess at a requirement that is unclear.

## 4. Branches and pull requests

| Branch | Purpose | Rule |
|---|---|---|
| `main` | Submission and presentation | Only the PM merges, and only from `dev` |
| `dev` | Integration (default branch) | Changes arrive only through reviewed pull requests |
| `feat/<role>-<task>` | New work | Pull request into `dev` |
| `fix/<role>-<task>` | Bug fixes | Pull request into `dev` |
| `docs/<topic>` | Documents | Pull request into `dev` |
| `exp/<role>-<topic>` | Experiments | No merge needed; record results in `experiments/` |

`<role>` is one of `model`, `serving`, `service`, `pm`.

- Never commit directly to `main` or `dev`.
- Each person opens pull requests for their own work from their own GitHub account.
- One pull request holds one change.
- Fill in `.github/PULL_REQUEST_TEMPLATE.md` in Korean. Describe only checks that were actually run.

Review and merge:

| Pull request | Approves | Merges |
|---|---|---|
| Model or serving work | The other member of the same role | The author |
| Service work | PM | The author |
| PM's `docs/` work | Any teammate | PM |
| "PM 확인 필요" checked (shared files, another role's folder, GPU or disk impact) | PM | PM |
| `dev` into `main` | — | PM only |

## 5. Commits

- Message: a prefix, then a Korean summary. Prefixes: `feat:`, `fix:`, `docs:`, `exp:`, `chore:`.
- Before the first commit on a machine, check that `git config user.name` and `user.email` are the person's own.

## 6. Dependencies

- A package goes into `requirements.txt` only when this repository's code imports it directly,
  or when a command these rules require runs it (`ruff`, `pytest`), or when running notebooks needs it (`ipykernel`).
- Nothing else goes in: not packages another package pulls in, not packages that might be needed later.
- When code stops importing a package, remove it from `requirements.txt` in the same change.
- `torch` is installed separately with its CUDA build, as `README.md` describes.

## 7. Language

| Text | Reader | Language |
|---|---|---|
| `AGENTS.md`, `CLAUDE.md` and other rules for coding agents | Coding agents | English |
| Python comments and docstrings | Team members | Korean |
| Notebook markdown cells and code comments | Team members | Korean |
| `README.md`, documents, pull request descriptions | Team members | Korean |

## 8. Cost and secrets

- Read keys from environment variables loaded from `.env`. Never hard-code or print them.
- The team's OpenAI budget is 30 USD in total, and the key is disabled the moment it is exceeded.
  Before running paid API calls in a loop or batch, show the estimated cost and wait for approval.
- Never open a port to the internet. Servers on the team VM are reached through SSH port forwarding only.

## 9. Team VM

The team shares one VM: NVIDIA L4 (24 GB), 16 GB RAM, 100 GB disk.
`docs/vm_member_guide.md` has the full rules for people.

- Work in your own clone under your home directory.
- Hugging Face models go only to `HF_HOME=/opt/shared/hf-cache`. Never download models into the repository.
- Before using the GPU, check `nvidia-smi` and remind the person to announce it in the team's `#gpu` channel.
  Stop processes and notebook kernels when done. Run long jobs inside `tmux`.
- Use only the person's own ports (`800N` for APIs, `850N` for UIs, see the member guide).
  `8000` and `8500` are for the team demo.
- Do not use `sudo`. System packages are installed by the VM admin.

## 10. Before every commit

1. `ruff check .` reports nothing.
2. `python -m pytest -q tests` passes. Until the first test exists, "no tests ran" (exit code 5) is expected.
3. Notebook outputs are cleared.
4. `git status` shows nothing from section 1.4.
