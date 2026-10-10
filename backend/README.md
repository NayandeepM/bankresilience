# BankResilience backend (MVP)

Runs on the host and controls only the `ledgerwriter` service in the Compose project `bank-of-anthos-local`.

## Prerequisites
- Docker Desktop running
- Bank of Anthos Compose project started and all 8 expected containers running
- Python 3.10+

## Start (PowerShell)
```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` to inspect the API. The API binds to loopback only. Do not expose it to your LAN: it has permission to stop/start an allowlisted container.

## Safety
- Only `ledgerwriter` is a fault target.
- The API requires explicit `confirm: true`.
- It refuses to run unless all eight expected Compose services are running.
- Only one experiment can run at a time.
- The fault window is capped at 12 seconds and the recovery is attempted in `finally`.
- It never deletes containers, images, or database volumes.

The first version verifies the container is running and the frontend responds after recovery. This is not proof that a transfer succeeded; manually verify a transaction in the browser and use the dashboard's transaction-check step before claiming full end-to-end recovery.
