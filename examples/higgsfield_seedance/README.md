# Higgsfield Seedance 2.5 smoke test

1. Create a key at console.higgsfield.ai (API access uses a separate prepaid balance, min $5).
2. Open `.env.local` in this folder and set `HF_KEY=<key-id>:<key-secret>` (it is git-ignored).
3. `cd backend && source venv/bin/activate && python examples/higgsfield_seedance/main.py`

Billable: one 5 s 720p text-to-video generation. Exit codes: 0 success, 1 failed/canceled/timeout, 2 credentials, 3 moderated.
