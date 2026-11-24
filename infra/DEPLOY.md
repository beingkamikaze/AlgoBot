# Running TradingBot on a server (for live trading)

Zerodha accepts API **orders** only from a whitelisted **static IP** (SEBI's retail-algo rules, April 2026).
Your Mac has no static IP, so live orders need a small server. Kite **data** (prices, login) works from the Mac.

Supabase stays where it is (hosted). The server runs three containers: Caddy (HTTPS), the web app and the backend.

> **Run only ONE backend against your Supabase at a time.** The paper and live loops run inside the backend; two
> backends (Mac + server) would both fill the same paper orders. Stop `./dev.sh` on the Mac once the server runs.

## 1. A server in India with a static IP

Any of these works; ~1 vCPU / 2 GB RAM is enough.

| Option | Static IP | Rough cost |
| --- | --- | --- |
| AWS Lightsail, Mumbai (ap-south-1), Ubuntu 24.04, 2 GB | attach a free Lightsail static IP | ~$12 / month |
| DigitalOcean, Bangalore (BLR1), 2 GB droplet | reserved IP | ~$12 / month |

Open ports 22, 80 and 443 only (Lightsail: Networking tab). Then on the server:

```bash
sudo apt update && sudo apt -y upgrade && sudo apt -y install unattended-upgrades ufw rsync
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw --force enable
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER
```

Use SSH keys only (`PasswordAuthentication no` in `/etc/ssh/sshd_config`).

## 2. A domain name

Point an A record (e.g. `trade.yourdomain.com`) at the static IP. Caddy gets the HTTPS certificate by itself.

## 3. Copy the app (from your Mac)

```bash
rsync -av --exclude '.venv' --exclude 'node_modules' --exclude '.next' --exclude 'backend/data' ~/TradingBot/ ubuntu@SERVER_IP:TradingBot/
```

## 4. Settings on the server

`backend/.env`: start from your Mac's file, then change:

```
REQUIRE_AUTH=true
ALLOWED_EMAILS=you@example.com
CORS_ORIGINS=https://trade.yourdomain.com
WEB_URL=https://trade.yourdomain.com
WEBHOOK_REQUIRE_TRADINGVIEW_IP=true
LIVE_TRADING_ENABLED=false          # true only when you are ready (step 8)
KITE_API_KEY=...
KITE_API_SECRET=...
TOKEN_ENCRYPTION_KEY=...            # a NEW key made on the server (see backend/.env.example)
```

`infra/.env` (copy `infra/.env.example`): your domain, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`.

## 5. Start it

```bash
cd ~/TradingBot && docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
docker compose -f infra/docker-compose.yml logs -f backend
```

Updates: rsync again, then the same `up -d --build`.

## 6. Supabase settings (dashboard)

- Authentication → URL Configuration: Site URL `https://trade.yourdomain.com`, add it to Redirect URLs.
- Authentication → Sign In / Providers: turn **off** "Allow new users to sign up" (your account already exists).
- Authentication → Multi-Factor: TOTP enabled (the default).
- SQL editor: run `supabase/migrations/0005_live.sql` if you have not.

## 7. Kite Connect app (developers.kite.trade, ₹500 / month)

- Redirect URL: `https://trade.yourdomain.com/api/live/kite/callback`
- Static IP: the server's IP (Kite allows a primary and a secondary).
- Copy the API key and secret into `backend/.env` on the server; restart: `docker compose ... up -d`.

## 8. The first live trade

1. Live page: log in to Kite, set up two-factor, check the gates are green except "armed".
2. Set live limits small (e.g. ₹200 per trade, ₹2,000 order value): the first trade should be a few shares.
3. Set `LIVE_TRADING_ENABLED=true`, restart, type LIVE to arm.
4. On a ticket: "Check this as a live order" → "Send to Zerodha". In Kite's order book you should see the LIMIT
   entry, and after the fill an SL order at your stop.
5. Close it from the Live page; compare the app's P&L with Kite's contract note that evening.
6. Keep minimum size for a few weeks with zero reconciliation alerts before raising limits.
