# Setup

Voraussetzungen: Python ≥ 3.11, Git, OpenAI-API-Key.

## 1. Von GitHub holen und installieren

```powershell
git clone https://github.com/ra0037I/mcp-inventory-agent.git
cd mcp-inventory-agent
python -m venv .venv
.venv\Scripts\Activate.ps1        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Konfigurieren

```powershell
copy .env.example .env            # Linux/macOS: cp .env.example .env
```

In der `.env` den eigenen Key eintragen:

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

## 3. Starten

```powershell
python agent.py --demo
```

Der Agent startet den MCP-Server selbst und arbeitet drei Demo-Szenarien ab.
Das Log liegt danach unter `logs/agent_run_<zeit>.log`.

Tests: `python -m pytest -q`

## Optional: über LiteLLM-Proxy

**Vorhandener Proxy:** in der `.env` Adresse und Key eintragen:

```ini
LLM_PROVIDER=litellm
LITELLM_API_BASE=http://<proxy-host>:4000
LITELLM_KEY=sk-<key-des-proxys>
```

**Kein Proxy verfügbar:** Ein eigener Proxy kann lokal installiert und gestartet werden.
`litellm[proxy]` ist bereits in `requirements.txt` enthalten. Der Proxy leitet an den OpenAI-Key aus der `.env` weiter.

1. Key erzeugen: `python -c "import secrets; print('sk-' + secrets.token_hex(24))"`
2. In der `.env` setzen (beide Keys identisch):
   ```ini
   LLM_PROVIDER=litellm
   LITELLM_API_BASE=http://localhost:4000
   LITELLM_KEY=sk-<key>
   LITELLM_MASTER_KEY=sk-<key>
   ```
3. Terminal 1: `python start_litellm.py`, Terminal 2: `python agent.py --demo`

