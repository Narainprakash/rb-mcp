# Hermes - Signal-Driven Options Trading Agent

Hermes is a self-hosted agent that polls an X (Twitter) account for options trading signals, parses them with zero latency using regex, applies predefined risk management and sizing rules, and automatically executes them via Robinhood's Agentic Trading MCP.

## Features
- **Zero-Latency Parsing**: Uses pure regex to parse highly structured signals, completely avoiding LLM latency and cost.
- **Strict Risk Controls**: Enforces maximum daily spend, per-trade limits, and maximum open positions.
- **Automated Take-Profit**: Automatically places configurable limit sell orders (e.g., 20% profit target) upon execution.
- **0DTE Protection**: Auto-cancels and market-sells open 0DTE limit orders at 15:50 ET to avoid total loss.
- **Hard Kill Switch**: An instant global kill switch via a simple `HALT` file presence check that immediately stops all trading.

---

## 1. Local Development Setup

### Prerequisites
- Python 3.10+
- SQLite3 (built-in with Python)

### Installation
1. First, create a private repository on GitHub (e.g., `hermes-agent`).
2. If you haven't already, initialize this local directory and push the code:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/hermes-agent.git
   git push -u origin main
   ```
3. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Set up configuration:
   - Copy `config.yaml.example` to `config.yaml` and modify as needed.
   - Copy `.env.example` to `.env` and fill in your API keys (Twitter, Discord, Robinhood).

### Running Locally (Paper Mode)
Ensure `paper_mode: true` is set in your `config.yaml` to prevent real trades.
```bash
python main.py
```

---

## 2. VPS Deployment Instructions (Production)

To deploy Hermes securely on a Linux VPS (e.g., Ubuntu 22.04), follow these steps:

### Step 2.1: VPS Security Lockdown
Before installing Hermes, secure your VPS:
1. **Disable Password Authentication**: Log in via SSH using your key.
2. **Create a Service User**: Never run Hermes as `root`.
   ```bash
   sudo adduser hermes
   sudo usermod -aG sudo hermes
   ```
3. **Configure Firewall (UFW)**: Only allow SSH. The web dashboard will use Cloudflare Tunnel.
   ```bash
   sudo ufw allow ssh
   sudo ufw enable
   ```

### Step 2.2: Install Hermes
1. Switch to the `hermes` user:
   ```bash
   su - hermes
   ```
2. Create an SSH key on your VPS and add it to your GitHub account to allow pulling from your private repository:
   ```bash
   ssh-keygen -t ed25519 -C "vps-hermes"
   cat ~/.ssh/id_ed25519.pub
   # Copy the output and add it to your GitHub Repo -> Settings -> Deploy Keys
   ```
3. Clone your private repository into the `hermes` user's home directory:
   ```bash
   git clone git@github.com:<your-username>/hermes-agent.git hermes
   cd hermes
   ```
4. Set up the Python virtual environment as described in the Local Setup.
4. Secure your `.env` file!
   ```bash
   chmod 600 .env
   ```

### Step 2.3: Set Up Systemd Services
We use `systemd` to run Hermes in the background and ensure it restarts automatically.

1. Create a systemd service file:
   ```bash
   sudo nano /etc/systemd/system/hermes.service
   ```
2. Add the following configuration (adjust paths if necessary):
   ```ini
   [Unit]
   Description=Hermes Trading Agent
   After=network.target

   [Service]
   Type=simple
   User=hermes
   WorkingDirectory=/home/hermes/hermes
   ExecStart=/home/hermes/hermes/venv/bin/python main.py
   Restart=always
   RestartSec=5
   EnvironmentFile=/home/hermes/hermes/.env

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable hermes
   sudo systemctl start hermes
   ```
4. View logs:
   ```bash
   journalctl -u hermes -f
   ```

### Step 2.4: The Kill Switch
If you need to instantly halt the system, create the `HALT` file in the root directory:
```bash
touch /home/hermes/hermes/HALT
```
To resume operations, simply remove the file:
```bash
rm /home/hermes/hermes/HALT
```

### Step 2.5: Dashboard Systemd Service
The dashboard is a read-only Flask web server. We run it as a separate service so it doesn't block the trading loop.

1. Create a systemd service for the dashboard:
   ```bash
   sudo nano /etc/systemd/system/hermes-dash.service
   ```
2. Add the following configuration:
   ```ini
   [Unit]
   Description=Hermes Web Dashboard
   After=network.target

   [Service]
   Type=simple
   User=hermes
   WorkingDirectory=/home/hermes/hermes
   ExecStart=/home/hermes/hermes/venv/bin/python src/dashboard/app.py
   Restart=always
   RestartSec=5
   EnvironmentFile=/home/hermes/hermes/.env

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable hermes-dash
   sudo systemctl start hermes-dash
   ```

### Step 2.6: Cloudflare Tunnel (External Access)
To securely access the web dashboard without opening ports:
1. Install `cloudflared` on the VPS.
2. Authenticate: `cloudflared tunnel login`
3. Create a tunnel: `cloudflared tunnel create hermes-dash`
4. Route traffic: Configure the tunnel to route to `http://127.0.0.1:8420`.
5. Run the tunnel as a systemd service.
