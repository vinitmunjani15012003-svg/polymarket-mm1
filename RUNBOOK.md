# Polymarket Market Maker - Runbook

## Deployment

The bot should be deployed via Docker in headless mode. 
A `docker-compose.yml` is provided for standard server deployments.

1. Copy `.env.example` to `.env` and fill in credentials.
2. Ensure `config/live.yaml` is configured with desired risk limits.
3. Run: `docker-compose up -d --build`

## Health Checks & Auto-restart

The `docker-compose.yml` includes:
- `restart: unless-stopped`: Automatically recovers from crashes.
- `healthcheck`: Checks the modification time of `data/state.json`. If state isn't written for 120s, Docker will restart the container.

## Handling Outages

If the bot crashes mid-session, do NOT panic.
State management (`src/execution/state_manager.py`) automatically persists:
1. Processed Fills
2. Inventory Balances
3. Open Orders

On restart, the bot will:
- Read `state.json`
- Send a `cancel_all` command to Polymarket to clear any lingering quotes.
- Reconcile loaded inventory with the current market cycle.
- Resume quoting automatically based on the restored inventory skew.

## Common Alerts

If `WEBHOOK_URL` is set, you may receive these alerts:
- **Toxicity Halt**: "repeated_adverse_fills". The bot detected it was being picked off and halted for `halt_cooldown` (60s). It will auto-resume.
- **Merge Failure**: Check Polygon RPC URL or MATIC balance. The bot needs gasless signing but underlying relayer issues can block merges.
- **Daily Loss Limit**: Bot will permanently halt for the day. Restart required to override.

## Stopping the Bot
To gracefully shut down and ensure all orders are cancelled:
```bash
docker-compose down
```
This triggers a `SIGTERM`, which the asyncio loop catches, calling `cancel_all` before exiting.
