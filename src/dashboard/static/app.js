document.addEventListener("DOMContentLoaded", () => {
    fetchHealth();
    fetchStats();
    fetchPositions();
    fetchHistory();
    fetchFeed();
    fetchLimitBuys();
    fetchEvents();
    fetchFailures();

    // Tab Logic
    const tabSignals = document.getElementById('tab-signals');
    const tabEvents = document.getElementById('tab-events');
    const feedSignals = document.getElementById('activity-feed');
    const feedEvents = document.getElementById('events-feed');

    tabSignals.addEventListener('click', () => {
        tabSignals.classList.add('active');
        tabEvents.classList.remove('active');
        feedSignals.style.display = 'flex';
        feedEvents.style.display = 'none';
    });

    tabEvents.addEventListener('click', () => {
        tabEvents.classList.add('active');
        tabSignals.classList.remove('active');
        feedEvents.style.display = 'flex';
        feedSignals.style.display = 'none';
    });

    // Poll every 5 seconds
    setInterval(() => {
        fetchHealth();
        fetchStats();
        fetchPositions();
        fetchHistory();
        fetchFeed();
        fetchLimitBuys();
        fetchEvents();
        fetchFailures();
    }, 5000);
});

async function fetchHealth() {
    try {
        const res = await fetch('/api/health');
        const data = await res.json();
        
        // System Status
        const dot = document.getElementById('system-dot');
        const text = document.getElementById('system-text');
        
        if (data.status === 'active') {
            dot.className = 'dot green';
            text.textContent = 'Active';
        } else {
            dot.className = 'dot red';
            text.textContent = 'HALTED';
        }
        
        // Mode Badge
        const modeBadge = document.getElementById('mode-badge');
        if (data.paper_mode) {
            modeBadge.textContent = 'PAPER';
            modeBadge.className = 'badge mode-paper';
        } else {
            modeBadge.textContent = 'LIVE';
            modeBadge.className = 'badge mode-live';
        }
        
        // Quota
        document.getElementById('quota-used').textContent = data.api_quota_used;
        document.getElementById('quota-limit').textContent = data.api_quota_limit;
        
        const bar = document.getElementById('quota-bar');
        bar.style.width = `${data.quota_pct}%`;
        
        if (data.quota_pct > 90) {
            bar.style.background = 'var(--accent-red)';
        } else if (data.quota_pct > 75) {
            bar.style.background = 'var(--accent-orange)';
        } else {
            bar.style.background = 'linear-gradient(to right, #3b82f6, #8b5cf6)';
        }
        
    } catch (e) {
        console.error("Failed to fetch health", e);
    }
}

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        
        const pnlEl = document.getElementById('daily-pnl');
        const sign = data.daily_pnl >= 0 ? '+' : '';
        pnlEl.textContent = `${sign}$${data.daily_pnl.toFixed(2)}`;
        pnlEl.className = data.daily_pnl >= 0 ? 'stat-value text-green' : 'stat-value text-red';
        
        document.getElementById('daily-wins').textContent = `${data.wins}W`;
        document.getElementById('daily-losses').textContent = `${data.losses}L`;
        
        const carEl = document.getElementById('capital-at-risk');
        carEl.textContent = `$${data.capital_at_risk.toFixed(2)}`;
        
    } catch (e) {
        console.error("Failed to fetch stats", e);
    }
}

async function fetchPositions() {
    try {
        const res = await fetch('/api/positions');
        const data = await res.json();
        const tbody = document.querySelector('#positions-table tbody');
        tbody.innerHTML = '';
        
        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-secondary)">No open positions</td></tr>';
            return;
        }
        
        data.forEach(pos => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><strong>$${pos.ticker}</strong> ${pos.expiry} ${pos.strike}${pos.option_type}</td>
                <td>${pos.total_quantity}</td>
                <td>$${pos.average_cost.toFixed(2)}</td>
                <td class="text-blue">$${pos.target_price ? pos.target_price.toFixed(2) : '-'}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to fetch positions", e);
    }
}

async function fetchHistory() {
    try {
        const res = await fetch('/api/history');
        const data = await res.json();
        const tbody = document.querySelector('#history-table tbody');
        tbody.innerHTML = '';
        
        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-secondary)">No closed trades yet</td></tr>';
            return;
        }
        
        data.forEach(trade => {
            const tr = document.createElement('tr');
            
            // Format time
            const date = new Date(trade.fill_timestamp + 'Z'); // SQLite timestamp is UTC if handled that way, or local. Assuming local.
            const timeStr = trade.fill_timestamp.split(' ')[1].substring(0, 5);
            
            const pnlClass = trade.realized_pnl >= 0 ? 'text-green' : 'text-red';
            const sign = trade.realized_pnl >= 0 ? '+' : '';
            
            tr.innerHTML = `
                <td><strong>$${trade.ticker}</strong> ${trade.expiry} ${trade.strike}${trade.option_type}</td>
                <td>${trade.total_quantity}</td>
                <td>${timeStr}</td>
                <td class="${pnlClass}">${sign}$${trade.realized_pnl.toFixed(2)}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to fetch history", e);
    }
}

async function fetchFeed() {
    try {
        const res = await fetch('/api/feed');
        const data = await res.json();
        const container = document.getElementById('activity-feed');
        container.innerHTML = '';
        
        if (data.length === 0) {
            container.innerHTML = '<div class="feed-item">Waiting for signals...</div>';
            return;
        }
        
        data.forEach(item => {
            const div = document.createElement('div');
            
            let itemClass = 'feed-item';
            if (item.action_taken === 'buy') itemClass += ' buy';
            else if (item.action_taken === 'skip') itemClass += ' skip';
            
            div.className = itemClass;
            
            const timeStr = item.alert_time.split(' ')[1].substring(0, 5);
            
            const header = `${item.action} $${item.ticker} ${item.strike}${item.option_type} @ ${item.rec_price}`;
            let body = "Processing...";
            if (item.reasoning) {
                body = `Decision: ${item.action_taken.toUpperCase()} - ${item.reasoning}`;
            }
            if (item.trade_status) {
                const mode = item.paper_mode ? "(Paper)" : "(LIVE)";
                body += ` <br><span class="text-blue">Executed ${mode}</span>`;
            }
            
            div.innerHTML = `
                <div class="feed-time">${timeStr} ET</div>
                <div class="feed-header">${header}</div>
                <div class="feed-body">${body}</div>
            `;
            container.appendChild(div);
        });
    } catch (e) {
        console.error("Failed to fetch feed", e);
    }
}

async function fetchLimitBuys() {
    try {
        const res = await fetch('/api/limit_buys');
        const data = await res.json();
        const tbody = document.querySelector('#limit-buys-table tbody');
        tbody.innerHTML = '';
        
        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; color:var(--text-secondary)">No pending dips</td></tr>';
            return;
        }
        
        data.forEach(order => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><strong>$${order.ticker}</strong> ${order.expiry} ${order.strike}${order.option_type}</td>
                <td>${order.quantity}</td>
                <td class="text-blue">$${order.target_price.toFixed(2)}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to fetch limit buys", e);
    }
}

async function fetchFailures() {
    try {
        const res = await fetch('/api/parse_failures');
        const data = await res.json();
        const tbody = document.querySelector('#failures-table tbody');
        tbody.innerHTML = '';
        
        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="2" style="text-align:center; color:var(--text-secondary)">All clear</td></tr>';
            return;
        }
        
        data.forEach(alert => {
            const tr = document.createElement('tr');
            const timeStr = alert.timestamp.split(' ')[1].substring(0, 5);
            tr.innerHTML = `
                <td>${timeStr}</td>
                <td style="color:var(--accent-orange); font-size:0.8rem;">${alert.raw_text.substring(0, 50)}...</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to fetch parse failures", e);
    }
}

async function fetchEvents() {
    try {
        const res = await fetch('/api/events');
        const data = await res.json();
        const container = document.getElementById('events-feed');
        container.innerHTML = '';
        
        if (data.length === 0) {
            container.innerHTML = '<div class="feed-item">No system events...</div>';
            return;
        }
        
        data.forEach(event => {
            const div = document.createElement('div');
            div.className = 'feed-item';
            
            const timeStr = event.timestamp.split(' ')[1].substring(0, 5);
            
            div.innerHTML = `
                <div class="feed-time">${timeStr} ET</div>
                <div class="feed-header">${event.event_type}</div>
                <div class="feed-body">${event.message}</div>
            `;
            container.appendChild(div);
        });
    } catch (e) {
        console.error("Failed to fetch events", e);
    }
}
