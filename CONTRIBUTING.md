# Contributing To LiF

Thanks for helping improve LiF. This project handles household finance data, so
changes should favor correctness, privacy, and explainability over speed.

## Local Setup

```bash
pipenv install
pipenv run python manage.py migrate
pipenv run python manage.py seed_demo
pipenv run python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`.

Install the tracked Git hooks once per checkout:

```bash
git config core.hooksPath .githooks
```

The hooks run the staged secret scan before commits and guard public releases
against accidental personal author or committer metadata.

## Before Opening A Pull Request

Run:

```bash
python3 scripts/scan_secrets.py
pipenv run python manage.py test
pipenv run python manage.py check
```

For deployment-related changes, also run:

```bash
pipenv run python manage.py check_production
```

## Engineering Expectations

- Keep real personal data out of commits.
- Keep money calculations in `Decimal`.
- Do not serialize money as JSON floats.
- Add regression tests for financial behavior changes.
- Add or update audit output when a new projection line affects cash, net
  worth, debts, depot holdings, or retirement outcomes.
- Keep user-facing features behind feature flags until they are ready.
- Mark new UI text for translation where practical.

See `ENGINEERING_GUARDRAILS.md` for the fuller set of project conventions.

## AI-Assisted Contributions

AI-assisted code and documentation are welcome, but the contributor remains
responsible for the result. Before opening a pull request, read the generated
changes, run the relevant tests and scans, and call out any financial behavior
that needs extra review. For projection logic, prefer small changes with clear
regression tests and audit output.

## Pull Request Shape

Prefer one logical change per pull request. A good PR includes:

- a short description of the user-facing or calculation change
- tests that cover the change
- notes about migration, deployment, or data-safety impact if relevant
- screenshots for meaningful UI changes

## Documentation

Useful starting points:

- `README.md` for setup and configuration
- `docs/USER_GUIDE.md` for app concepts
- `docs/ADMIN_GUIDE.md` for operations and self-hosting
- `docs/I18N.md` for translation workflow
- `docs/HOME_ASSISTANT_ADDON.md` for the experimental Home Assistant add-on plan
