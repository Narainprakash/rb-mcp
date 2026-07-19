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
3. **Configure Firewall (UFW)**: Only allow SSH. The web dashboard will be accessed securely via Tailscale.
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

### Step 2.7: Tailscale VPN (External Access)
To securely access the web dashboard without opening ports or buying a domain, we use Tailscale to create a private network between your devices:
1. Create a free account at [Tailscale](https://tailscale.com).
2. Install Tailscale on the VPS:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
3. Follow the authentication link printed in the terminal to add the VPS to your Tailscale network.
4. Install the Tailscale app on your local computer/phone and log in.
5. Update your `config.yaml` to allow incoming Tailscale connections by changing the `bind_address` to `0.0.0.0`:
   ```yaml
   dashboard:
     bind_address: "0.0.0.0"
     port: 8420
   ```
6. Restart the dashboard service: `sudo systemctl restart rb-mcp-dash`
7. You can now securely access the dashboard by navigating to `http://<vps-tailscale-ip>:8420` in your browser.

---

## 3. Nous Hermes Agent Integration

This trading bot runs on the [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) open-source AI agent framework. The Hermes Agent provides a conversational interface (via Telegram/Discord/CLI) powered by an LLM of your choice (via OpenRouter) to monitor and manage the trading bot in natural language.

### 3.1 Install Hermes Agent on VPS
**IMPORTANT**: Ensure you run these commands as the `rb-mcp-user` (not `root`).

```bash
# Install the framework
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc

# Configure LLM provider (interactive — select OpenRouter, enter API key, pick model)
# NOTE: Running this command for the first time will auto-generate the 
# ~/.hermes/ directory and the ~/.hermes/config.yaml file.
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
mkdir -p ~/.hermes/custom_tools
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

---

## 4. Connecting the Robinhood MCP (Live Trading)

> **⚠️ WARNING**: Do NOT proceed with this section until you have run the bot in Paper Mode for at least 1–2 weeks and validated parsing accuracy against real signals. You are responsible for every trade the agent places.

### 4.1 Create a Robinhood Agentic Account
Robinhood's agentic trading uses a **separate, sandboxed sub-account** — it will never touch your main investing portfolio.

1. On a **desktop browser**, log in to [robinhood.com](https://robinhood.com).
2. Navigate to **Account → Agentic Trading** and create a new Agentic account.
3. Transfer funds into the Agentic account. Start small (e.g., $100–$500) while testing.

> **NOTE**: The initial authentication **must be done on a desktop device**. If you see the onboarding link on your phone, copy it and open it in a desktop browser.

### 4.2 Register the Robinhood MCP Server
Add the official Robinhood MCP endpoint to your Hermes Agent config (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  robinhood:
    url: "https://agent.robinhood.com/mcp/trading"
    auth: "oauth"
    enabled: true
```

After adding this, restart the Hermes Agent's gateway process so it loads the new MCP server:
```bash
hermes gateway restart
```
This will trigger the browser-based authentication flow. Complete it when prompted to cache a session token locally.

> **💡 HEADLESS VPS TIP**: If you are SSH'd into a remote server, Hermes Agent will detect the remote session and print a secure `https://robinhood.com/oauth?...` link in your terminal.
> 1. Copy that URL and open it in your desktop browser.
> 2. Authorize the connection to your Agentic account.
> 3. Your browser will try to redirect to localhost and fail (e.g., "Site can't be reached"). This is expected!
> 4. Copy the entire URL from your browser's address bar (it contains the `?code=...` parameter) and paste it back into your VPS terminal prompt. Hermes will instantly grab the token and save it.

### 4.3 Verify the Connection
Run the MCP list command to confirm the Robinhood MCP is connected:

```bash
hermes mcp list
```

You should see the `robinhood` MCP server listed and its status. You can also test the connection explicitly using `hermes mcp test robinhood`, or test it from the Hermes chat:

> *"What's my Robinhood account balance?"*

### 4.4 Switch from Paper Mode to Live Mode
Once you've verified the MCP connection is healthy:

1. Edit your project's `config.yaml`:
   ```yaml
   execution:
     paper_mode: false
   ```
2. Restart the `rb-mcp` systemd service:
   ```bash
   sudo systemctl restart rb-mcp
   ```
3. **Start with very low limits** in `config.yaml`:
   ```yaml
   decision:
     max_daily_spend_usd: 100
     per_trade_max_spend_usd: 50
   ```
   Raise these gradually as you gain confidence.

### 4.5 Disconnect / Emergency Off
You have three independent ways to cut trading access:

| Method | How | When to use |
|---|---|---|
| **Kill Switch (fastest)** | `touch ~/rb-mcp/HALT` | Bot is running, you want to stop it instantly |
| **Hermes Agent** | Message: *"Stop all trading immediately"* | From your phone via Telegram/WhatsApp |
| **Robinhood App** | Account → Agentic Trading → Disconnect | Nuclear option — revokes all agent access at the broker level |

---

## 5. Monitoring & Dashboard

### 5.1 Hermes Agent Dashboard (Web)
The Hermes Agent includes a built-in web dashboard. Because it builds the UI using React, your VPS must have Node.js installed first.

1. **Install Node.js (Ubuntu/Debian):**
   ```bash
   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
   sudo apt install -y nodejs
   ```

2. **Launch the Dashboard:**
   ```bash
   hermes dashboard
   ```

> **🛠️ FIXING "npm install failed"**: If you get a "Web UI npm install failed" error when launching the dashboard, it is because Hermes is installed globally and your user doesn't have permission to build the UI files. To fix it, build it manually with `sudo`:
> ```bash
> cd /usr/local/lib/hermes-agent
> sudo npm install --workspace web
> sudo npm run build -w web
> cd ~/rb-mcp
> hermes dashboard
> ```

This starts a local web interface (default `http://127.0.0.1:9119`) where you can:
- View agent status, gateway health, and session counts
- Manage cron jobs, API keys, and skills
- Chat with the agent directly from the browser

### 5.2 Accessing from Your Phone
There is **no official Hermes Agent mobile app**. However, you can access the web dashboard from your phone's browser using one of these methods:

**Option A — Tailscale (already set up in Step 2.7):**
Ensure the Tailscale app is installed and connected on your phone. You can access the dashboard directly via the VPS's Tailscale IP:
```
http://<vps-tailscale-ip>:9119
```

**Option C — SSH Port Forwarding (Easiest for Desktop):**
If you want to access the dashboard on your computer while SSH'd into the VPS, open a *new* terminal window on your local machine and run:
```bash
ssh -L 9119:127.0.0.1:9119 rb-mcp-user@<your_vps_ip>
```
Leave that window open. You can now open `http://127.0.0.1:9119` in your local browser!

### 5.3 Trading Bot Dashboard (Project-Specific)
The project's own read-only web dashboard (`rb-mcp-dash` systemd service) runs on port `8420` and provides:
- Alert history and parse results
- Trade log with decision reasoning
- Open positions and P/L summary
- API call counter (X API quota tracking)

Access it securely via the Tailscale IP configured in Step 2.7 (e.g., `http://<vps-tailscale-ip>:8420`).
