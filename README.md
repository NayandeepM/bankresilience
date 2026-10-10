# BankResilience MVP - local multi-agent reliability demo

This is a starter control plane and dashboard for the local Bank of Anthos Compose project.

## What is implemented
- Live Docker Compose container status, frontend HTTP probe, and a service dependency view.
- Four controlled failure types: crash `ledgerwriter`, pause `contacts`, disconnect `transactionhistory` from its Compose network, and a traffic-level request timeout through a local proxy.
- Deterministic diagnosis based on the injected action and captured container state.
- Automatic recovery for every experiment in a `finally` path.
- SQLite experiment history and an event timeline.
- Explicit UI confirmation, one experiment at a time, baseline preflight, and a strict service allowlist.

## Important limitation
The control plane verifies that every expected container is running, the disconnected network is restored, and the frontend responds after recovery. It does not automatically perform a banking transfer or prove transaction correctness. After recovery, manually test a small demo transfer in the banking UI. Do not claim end-to-end recovery until that succeeds.

## Prerequisites
- Docker Desktop running
- Bank of Anthos Compose project started with all eight expected services for the three Docker experiments; the timeout experiment can use the included stub instead.
- Python 3.10+
- Node.js 20+ (or current supported LTS)

This repository does not include Bank of Anthos itself. By default it expects Compose project `bank-of-anthos-local` and banking UI `http://localhost:8082`. If another desktop uses different values, configure them before starting the backend:

```bash
export BANKRESILIENCE_COMPOSE_PROJECT=bank-of-anthos-local
export BANKRESILIENCE_FRONTEND_URL=http://localhost:8082
```

## Run the timeout experiment without Docker

The proxy always forwards to the single `UPSTREAM_URL` configured when the backend starts. The timeout experiment needs only a reachable HTTP upstream, not the eight Bank of Anthos containers.

1. In one terminal at the repository root, start the included stub: `python3 scripts/stub_bank.py` (Windows: `py scripts/stub_bank.py`). It listens on `127.0.0.1:8082` by default. Use `--port 8090` or set `PORT=8090` to change the port.
2. Start the backend as below. If the stub uses another port, set `UPSTREAM_URL=http://127.0.0.1:8090` in the backend terminal first (PowerShell: `$env:UPSTREAM_URL="http://127.0.0.1:8090"`).
3. Start the dashboard and run **Request timeout injection**. Other experiment buttons require the Bank of Anthos Compose stack.
4. Watch the 12-second fault window and the timeout results panel. The run and its measurements are saved to `backend/experiments.db`.

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

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1` and run the same `python -m unittest discover -s tests -v` command. The integration test starts and stops its own stub and backend on free local ports.

## First test
1. For Docker experiments, confirm all eight Bank of Anthos services are running and login works at the configured banking URL. For the timeout experiment, start the stub as above.
2. Open BankResilience dashboard.
3. Run the available experiment cards and accept each confirmation.
4. Watch the timeline and diagnosis/recovery panel.
5. When the experiment finishes, return to Bank of Anthos and manually verify a transfer and transaction history.

## Safety
- Backend binds to loopback (`127.0.0.1`), not the LAN.
- Only `ledgerwriter`, `contacts`, and `transactionhistory` are fault targets.
- The three Docker experiments refuse to start unless all eight expected Compose services are running. The timeout experiment checks only its configured upstream.
- Only one experiment can run at a time; the configurable fault window is always capped at 12 seconds.
- Never expose this control API to an untrusted network. Docker control is powerful.
- It never deletes containers, images, or database volumes.
