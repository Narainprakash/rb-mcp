# Hermes - Signal-Driven Options Trading Agent

Hermes is a self-hosted agent that polls an X (Twitter) account for options trading signals, parses them with zero latency using regex, applies predefined risk management and sizing rules, and automatically executes them via Robinhood's Agentic Trading MCP.

## Features
- **Zero-Latency Parsing**: Uses pure regex to parse highly structured signals, completely avoiding LLM latency and cost.
- **Strict Risk Controls**: Enforces maximum daily spend, per-trade limits, and maximum open positions.
- **Discount Limit Buys**: If the live market ask is cheaper than the alert price, the bot places a limit buy order at a configurable discount (e.g., 20% lower) to catch dips, rather than buying at market.
- **Automated Take-Profit**: Automatically places configurable limit sell orders (e.g., 20% profit target) upon execution.
- **0DTE Protection**: Auto-cancels and market-sells open 0DTE limit orders at 15:50 ET to avoid total loss.
- **Hard Kill Switch**: An instant global kill switch via a simple `HALT` file presence check that immediately stops all trading.

---

## 1. Local Development Setup

### Prerequisites
- Python 3.10+
- SQLite3 (built-in with Python)

1. If you haven't already, push your local code to your GitHub repository:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/Narainprakash/rb-mcp.git
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
   sudo adduser rb-mcp-user
   sudo usermod -aG sudo rb-mcp-user
   ```
3. **Configure Firewall (UFW)**: Only allow SSH. The web dashboard will use Cloudflare Tunnel.
   ```bash
   sudo ufw allow ssh
   sudo ufw enable
   ```

### Step 2.2: Install NousResearch Hermes Agent
Install the agent framework that will host the trading bot:
```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc
```

### Step 2.3: Clone & Set Up the Trading Bot
1. Switch to the `rb-mcp-user` user:
   ```bash
   su - rb-mcp-user
   ```
2. Create an SSH key on your VPS and add it to your GitHub account to allow pulling from your private repository:
   ```bash
   ssh-keygen -t ed25519 -C "vps-rb-mcp"
   cat ~/.ssh/id_ed25519.pub
   # Copy the output and add it to your GitHub Repo -> Settings -> Deploy Keys
   ```
3. Clone your repository into the `rb-mcp-user` user's home directory:
   ```bash
   git clone git@github.com:Narainprakash/rb-mcp.git rb-mcp
   cd rb-mcp
   ```
4. Set up the Python virtual environment as described in the Local Setup.
4. Secure your `.env` file!
   ```bash
   chmod 600 .env
   ```

### Step 2.4: Set Up Systemd Services
We use `systemd` to run Hermes in the background and ensure it restarts automatically.

1. Create a systemd service file:
   ```bash
   sudo nano /etc/systemd/system/rb-mcp.service
   ```
2. Add the following configuration (adjust paths if necessary):
   ```ini
   [Unit]
   Description=Hermes Trading Agent
   After=network.target

   [Service]
   Type=simple
   User=rb-mcp-user
   WorkingDirectory=/home/rb-mcp-user/rb-mcp
   ExecStart=/home/rb-mcp-user/rb-mcp/venv/bin/python main.py
   Restart=always
   RestartSec=5
   EnvironmentFile=/home/rb-mcp-user/rb-mcp/.env

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable rb-mcp
   sudo systemctl start rb-mcp
   ```
4. View logs:
   ```bash
   journalctl -u rb-mcp -f
   ```

### Step 2.5: The Kill Switch
If you need to instantly halt the system, create the `HALT` file in the root directory:
```bash
touch /home/rb-mcp-user/rb-mcp/HALT
```
To resume operations, simply remove the file:
```bash
rm /home/rb-mcp-user/rb-mcp/HALT
```

### Step 2.6: Dashboard Systemd Service
The dashboard is a read-only Flask web server. We run it as a separate service so it doesn't block the trading loop.

1. Create a systemd service for the dashboard:
   ```bash
   sudo nano /etc/systemd/system/rb-mcp-dash.service
   ```
2. Add the following configuration:
   ```ini
   [Unit]
   Description=Hermes Web Dashboard
   After=network.target

   [Service]
   Type=simple
   User=rb-mcp-user
   WorkingDirectory=/home/rb-mcp-user/rb-mcp
   ExecStart=/home/rb-mcp-user/rb-mcp/venv/bin/python src/dashboard/app.py
   Restart=always
   RestartSec=5
   EnvironmentFile=/home/rb-mcp-user/rb-mcp/.env

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable rb-mcp-dash
   sudo systemctl start rb-mcp-dash
   ```

### Step 2.7: Cloudflare Tunnel (External Access)
To securely access the web dashboard without opening ports:
1. Install `cloudflared` on the VPS.
2. Authenticate: `cloudflared tunnel login`
3. Create a tunnel: `cloudflared tunnel create hermes-dash`
4. Route traffic: Configure the tunnel to route to `http://127.0.0.1:8420`.
5. Run the tunnel as a systemd service.

---

## 3. Nous Hermes Agent Integration

This trading bot runs on the [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) open-source AI agent framework. The Hermes Agent provides a conversational interface (via Telegram/Discord/CLI) powered by an LLM of your choice (via OpenRouter) to monitor and manage the trading bot in natural language.

### 3.1 Install Hermes Agent on VPS
```bash
# Install the framework
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc

# Configure LLM provider (interactive — select OpenRouter, enter API key, pick model)
hermes model

# Connect Telegram gateway (authorized user: @Prakash_1803)
hermes gateway setup

# Verify everything is healthy
hermes doctor
```

### 3.2 Register Custom Tools
The custom tools in `src/hermes_agent_tools/trading_manager.py` give the agent read/write access to the trading bot's state.

**Method A — Symlink (recommended):**
```bash
ln -s ~/rb-mcp/src/hermes_agent_tools ~/.hermes/custom_tools/hermes-trading
```

**Method B — Config entry:**
Add to `~/.hermes/config.yaml`:
```yaml
skills:
  external_dirs:
    - ~/rb-mcp/src/hermes_agent_tools
```

### 3.3 Available Tools

| Tool | Description | Example Prompt |
|---|---|---|
| `get_open_positions()` | Returns open option positions | *"What positions are we holding?"* |
| `get_todays_realized_pnl()` | Today's realized P/L | *"How much did we make today?"* |
| `get_system_status()` | Bot status + API quota | *"Is the bot running?"* |
| `trigger_kill_switch()` | Creates `HALT` file | *"Stop all trading immediately."* |
| `resume_trading()` | Removes `HALT` file | *"Resume the trading bot."* |

### 3.4 LLM Configuration (OpenRouter)

Store your API key in `~/.hermes/.env` (**not** in the project's `.env`):
```
OPENROUTER_API_KEY=sk-or-your-actual-key-here
```

Configure the provider in `~/.hermes/config.yaml`:
```yaml
model:
  provider: openrouter
  default: "nousresearch/hermes-3-llama-3.1-405b"
```

Switch models on the fly inside the chat: `/model anthropic/claude-sonnet-4-20250514`

Browse models at [openrouter.ai/models](https://openrouter.ai/models).
