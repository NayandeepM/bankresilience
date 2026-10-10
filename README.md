# BankResilience MVP - local multi-agent reliability demo

This is a starter control plane and dashboard for the local Bank of Anthos Compose project.

## What is implemented
- Live Docker Compose container status, frontend HTTP probe, and a service dependency view.
- Three controlled failure types: crash `ledgerwriter`, pause `contacts`, and disconnect `transactionhistory` from its Compose network.
- Deterministic diagnosis based on the injected action and captured container state.
- Automatic recovery for every experiment in a `finally` path.
- SQLite experiment history and an event timeline.
- Explicit UI confirmation, one experiment at a time, baseline preflight, and a strict service allowlist.

## Important limitation
The control plane verifies that every expected container is running, the disconnected network is restored, and the frontend responds after recovery. It does not automatically perform a banking transfer or prove transaction correctness. After recovery, manually test a small demo transfer in the banking UI. Do not claim end-to-end recovery until that succeeds.

## Prerequisites
- Docker Desktop running
- Bank of Anthos Compose project started with all eight expected services
- Python 3.10+
- Node.js 20+ (or current supported LTS)

This repository does not include Bank of Anthos itself. By default it expects Compose project `bank-of-anthos-local` and banking UI `http://localhost:8082`. If another desktop uses different values, configure them before starting the backend:

```bash
export BANKRESILIENCE_COMPOSE_PROJECT=bank-of-anthos-local
export BANKRESILIENCE_FRONTEND_URL=http://localhost:8082
```

## Run backend (macOS/Linux terminal 1)
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

On Windows PowerShell, activate the environment with `.\.venv\Scripts\Activate.ps1`.

## Run dashboard (terminal 2)
```bash
cd frontend
npm install
npm run dev
```

Open the local URL Vite prints, usually `http://127.0.0.1:5173`. If the backend runs at a different address, set `VITE_API_URL` before `npm run dev`.

## Verify before a demo
```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s tests -v

cd ../frontend
npm run build
```

## First test
1. Confirm all eight Bank of Anthos services are running and login works at `http://localhost:8082`.
2. Open BankResilience dashboard.
3. Run each of the three experiment cards and accept each confirmation.
4. Watch the timeline and diagnosis/recovery panel.
5. When the experiment finishes, return to Bank of Anthos and manually verify a transfer and transaction history.

## Safety
- Backend binds to loopback (`127.0.0.1`), not the LAN.
- Only `ledgerwriter`, `contacts`, and `transactionhistory` are fault targets.
- The API refuses to start unless all eight expected Compose services are running.
- Only one experiment can run at a time; the configurable fault window is always capped at 12 seconds.
- Never expose this control API to an untrusted network. Docker control is powerful.
- It never deletes containers, images, or database volumes.
