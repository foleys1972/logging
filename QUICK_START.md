# Quick Start Guide

## Prerequisites
- Python 3.10+
- Node.js 18+
- PostgreSQL (or use SQLite for testing)

## 1. Control Plane Setup

### Using SQLite (easiest for testing)
```bash
cd control_plane

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# or: source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Create a .env file with SQLite
echo DATABASE_URL=sqlite+aiosqlite:///./wba_monitor.db > .env
echo AGENT_API_TOKENS=test-agent-token >> .env
echo DASHBOARD_API_TOKENS=test-dashboard-token >> .env

# Run the control plane
python -m uvicorn app.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`

### Using PostgreSQL
```bash
# Install PostgreSQL driver
pip install asyncpg

# Create database
createdb wba_monitor

# Update .env
DATABASE_URL=postgresql+asyncpg://wba:wba@localhost:5432/wba_monitor
AGENT_API_TOKENS=test-agent-token
DASHBOARD_API_TOKENS=test-dashboard-token
```

## 2. Seed Test Data

Create a site and some sample snapshots:

```bash
# Create a test site (POST http://localhost:8000/api/v1/sites)
curl -X POST http://localhost:8000/api/v1/sites \
  -H "Authorization: Bearer test-dashboard-token" \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Site", "region": "EMEA"}'

# Note the site_id returned (probably 1)
```

Send a test snapshot (replace SITE_ID with the actual ID):

```bash
curl -X POST http://localhost:8000/api/v1/sites/1/snapshots \
  -H "Authorization: Bearer test-agent-token" \
  -H "Content-Type: application/json" \
  -d '{
    "component": "get_zones",
    "hash": "abc123",
    "count": 5,
    "metrics": {"zone_count": 5},
    "flags": {}
  }'

curl -X POST http://localhost:8000/api/v1/sites/1/snapshots \
  -H "Authorization: Bearer test-agent-token" \
  -H "Content-Type: application/json" \
  -d '{
    "component": "get_tpos",
    "hash": "def456",
    "count": 3,
    "metrics": {"tpo_count": 3, "alive": 3},
    "flags": {}
  }'
```

## 3. Dashboard Setup

```bash
cd dashboard

# Install dependencies
npm install

# Run development server
npm run dev
```

The dashboard will open at `http://localhost:5173` and automatically proxy API requests to `http://localhost:8000`

## 4. View the Dashboard

Open your browser to `http://localhost:5173`. You should see:
- Sidebar with "TradeSense Atlas" branding
- Aggregate cards showing site counts
- Site tiles with RAG status
- Click any site tile to see component details in the side panel

## 5. Run the Agent (Optional)

To connect a real agent:

```bash
cd ..  # back to logging/

# Create agent config
python -c "
from wba_agent.config import AgentConfig, SiteConfig, ControlPlaneConfig
from wba_agent.config import save_config
from pathlib import Path

config = AgentConfig(
    interval_minutes=5,
    sites=[
        SiteConfig(
            name='My WBA Site',
            url='wss://your-wba-host/api',
            token='your-token',
            control_plane_site_id=1,
            auto_start=True
        )
    ],
    control_plane=ControlPlaneConfig(
        base_url='http://localhost:8000',
        api_token='test-agent-token'
    )
)
save_config(config, Path('agent_config.json'))
print('Created agent_config.json')
"

# Install agent dependencies (from parent directory)
cd ..
pip install -r wba_agent/requirements.txt
cd wba_agent

# Run agent
python -m wba_agent.cli --config agent_config.json
```

## Troubleshooting

### "Module not found" errors
Run `pip install -r requirements.txt` from the appropriate directory:
- Control Plane: `cd control_plane && pip install -r requirements.txt`
- Agent: `pip install -r wba_agent/requirements.txt`

### Database connection errors
Make sure PostgreSQL is running OR switch to SQLite by setting `DATABASE_URL=sqlite+aiosqlite:///./wba_monitor.db`

### Dashboard can't reach API
Check that the control plane is running on port 8000. The dashboard proxy is configured in `dashboard/vite.config.ts`

### Auth errors
Make sure you're using the correct tokens:
- Agent endpoints need: `test-agent-token`
- Dashboard endpoints need: `test-dashboard-token`

You can also comment out the auth decorators in `control_plane/app/api/v1/endpoints.py` for development if needed.

