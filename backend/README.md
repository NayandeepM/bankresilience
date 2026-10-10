# BankResilience backend (MVP)

Runs on the host and controls only `ledgerwriter`, `contacts`, and `transactionhistory` in an allowlisted Bank of Anthos Compose project.

## Prerequisites
- Docker Desktop running
- Bank of Anthos Compose project started and all 8 expected containers running
- Python 3.10+

## Start (macOS/Linux)
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` to inspect the API. The API binds to loopback only. Do not expose it to your LAN: it has permission to stop/start an allowlisted container.

Optional settings for machines where the Compose project or banking UI uses a different name or port:

```bash
export BANKRESILIENCE_COMPOSE_PROJECT=bank-of-anthos-local
export BANKRESILIENCE_FRONTEND_URL=http://localhost:8082
export BANKRESILIENCE_FAULT_WINDOW_SECONDS=12
```

The fault window setting is always capped at 12 seconds.

## Safety
- Only `ledgerwriter`, `contacts`, and `transactionhistory` are fault targets.
- The API requires explicit `confirm: true`.
- It refuses to run unless all eight expected Compose services are running.
- Only one experiment can run at a time.
- The fault window is capped at 12 seconds and the recovery is attempted in `finally`.
- Recovery passes only when the target and all eight services are running, the network is restored, and the banking frontend responds.
- It never deletes containers, images, or database volumes.

The control plane verifies container, network, and frontend recovery. This is not proof that a transfer succeeded; manually verify a transaction in the browser before claiming full end-to-end recovery.
