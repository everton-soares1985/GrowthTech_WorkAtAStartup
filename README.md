# GrowthTech Work at a Startup

Standalone, supervised automation for the internal application flow at
[Work at a Startup](https://www.workatastartup.com/).

The project preserves the filters selected by the operator, scans jobs in the
visible order, skips unsupported forms, generates a truthful application
message, submits only with an explicit execution flag, and requires the visual
state `Applied` before counting success.

## Safety boundary

- No submission without `--execute-apply`.
- Complex or ambiguous forms are closed with `Escape` and skipped.
- CAPTCHA, login challenges and uncertain post-submit states stop the batch.
- `.env`, the candidate profile, browser session and reports are ignored by Git.
- The automation does not rank or reject jobs based on candidate fit.

## Setup

```powershell
cd C:\PROGRAMACAO\GrowthTech_WorkAtAStartup
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Insert `OPENROUTER_API_KEY` in `.env`, then open the dedicated browser:

```powershell
.\scripts\start_chrome.bat
```

After the login is saved, headless mode is available:

```powershell
.\scripts\start_chrome.bat headless
```

## Validation

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest
.\.venv\Scripts\python.exe -X utf8 src\workatastartup_apply.py --self-check
.\.venv\Scripts\python.exe -X utf8 src\workatastartup_apply.py --list-readonly --max-jobs 10
```

## Controlled application batch

```powershell
.\.venv\Scripts\python.exe -X utf8 src\workatastartup_apply.py `
  --apply-batch --execute-apply `
  --max-jobs 1 --max-scan 20
```

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for operational details.
