# BankResilience MVP — local multi-agent reliability demo

This is a starter control plane and dashboard for the local Bank of Anthos Compose project.

## What is implemented
- Live Docker Compose container status, frontend HTTP probe, and a service dependency view.
- One controlled experiment: stop only `ledgerwriter` for a 12-second observation window.
- Deterministic diagnosis based on the successful stop action and captured container state.
- Automatic attempt to restart `ledgerwriter` in a `finally` path.
- SQLite experiment history and an event timeline.
- Explicit UI confirmation, one experiment at a time, baseline preflight, and a strict service allowlist.

## Important limitation
The first version verifies that the LedgerWriter container is running and that the frontend responds after recovery. It does not automatically perform a banking transfer or prove transaction correctness. After recovery, manually test a small demo transfer in `http://localhost:8082`. Do not claim end-to-end recovery until that succeeds.

## Prerequisites
- Docker Desktop running
- Bank of Anthos Compose project started in project `bank-of-anthos-local`
- Python 3.10+
- Node.js 20+ (or current supported LTS)

## Run backend (PowerShell terminal 1)
```powershell
cd .\backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` and check `/api/health`.

## Run dashboard (PowerShell terminal 2)
```powershell
cd .\frontend
npm install
npm run dev
```
Open the local URL Vite prints, usually `http://127.0.0.1:5173`.

## First test
1. Confirm all eight Bank of Anthos services are running and login works at `http://localhost:8082`.
2. Open BankResilience dashboard.
3. Click **Run LedgerWriter experiment** and accept the confirmation.
4. Watch the timeline and diagnosis/recovery panel.
5. When the experiment finishes, return to Bank of Anthos and manually verify a transfer and transaction history.

## Safety
- Backend binds to loopback (`127.0.0.1`), not the LAN.
- Only `ledgerwriter` is a fault target.
- The API refuses to start unless all eight expected Compose services are running.
- Only one experiment can run at a time; fault window is capped at 12 seconds.
- Never expose this control API to an untrusted network. Docker control is powerful.
- It never deletes containers, images, or database volumes.
