# MCP Inventory Agent: eigener MCP-Server für Datenbank- und Rechen-Tools

**Industrial Computing, Homework Teil 1, Option A (Technical Track)**
MCI Master, Andreas Rofner

Das Projekt erweitert die Code-Beispiele der Vorlesung ([chmaurer/mci_industrial_computing](https://github.com/chmaurer/mci_industrial_computing), *Sample 3 MCP* und *Sample 4 MCP Architecture*) um:

1. einen eigenen **MCP-Server** (Python, FastMCP) mit **3 Tools**, dazu Resources und einen Prompt,
2. einen **ReAct-Agent**, der die Tools zur Laufzeit über MCP entdeckt und im Zyklus *Thought → Action → Observation* arbeitet. Das LLM ist über die `.env` wählbar: OpenAI direkt, ein **LiteLLM-Proxy**, Azure OpenAI oder ein lokales Ollama-Modell.

## Schnellstart

Ausführliche Schritte: **[SETUP.md](SETUP.md)**

```powershell
git clone https://github.com/ra0037I/mcp-inventory-agent.git
cd mcp-inventory-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env            # OPENAI_API_KEY eintragen
python agent.py --demo
```

## Architektur

```
┌────────────────────────────── agent.py (MCP-Host + Client) ─────────────────────────────┐
│  ReAct-Schleife:  Thought ──► Action (Tool-Call) ──► Observation ──► … ──► Final Answer │
│        ▲                         │                    ▲                                 │
│        │ OpenAI-kompatible API   │ MCP (stdio oder Streamable HTTP)                     │
│  ┌─────┴──────────────┐   ┌──────▼────────────────────┴─────────────────────────────┐   │
│  │ LLM (llm_provider) │   │ server.py  "MCI-Inventory-Tools"  (FastMCP)             │   │
│  │ openai | litellm | │   │  Tools:     query_inventory ──────────► SQLite (data/)  │   │
│  │ azure | ollama     │   │             calculate_tiered_discount ► discount_engine │   │
│  └────────────────────┘   │             append_audit_log ─────────► data/audit.log  │   │
│                           │  Resources: audit://events, audit://events/{n},         │   │
│                           │             inventory://schema                          │   │
│                           │  Prompts:   sales_quote_workflow                        │   │
│                           └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

**Ablauf:** `agent.py` startet `server.py` als Unterprozess und fragt per MCP die verfügbaren Tools, Resources und Prompts ab. Die Tool-Beschreibungen gehen zusammen mit der Frage an das LLM. Das LLM antwortet mit einem Tool-Call (*Action*). Der Agent führt ihn über MCP auf dem Server aus und gibt das Ergebnis (*Observation*) an das LLM zurück. Das wiederholt sich, bis das LLM eine *Final Answer* liefert.

## Die drei MCP-Tools

| Tool | Zweck | Umsetzung |
|---|---|---|
| `query_inventory` | SQL-Abfrage des Produktbestands nach SKU, Freitext, Kategorie, Mindestbestand oder Mindestmenge | `sqlite3` mit parametrisierten Queries und Whitelist für `ORDER BY` (sicher gegen SQL-Injection). Die DB mit 14 Industrieprodukten wird automatisch angelegt |
| `calculate_tiered_discount` | Formel-Engine für **Staffelrabatte nach Bestellmenge** | Mengenstufen, Kundenstufen-Bonus, Bestellwert-Bonus und Obergrenze je Kategorie (siehe unten). Bei einer SKU kommen Preis und Kategorie aus der DB, dazu ein Lager-Check |
| `append_audit_log` | Schreibt Ereignisse in eine **lokale Datei** | Append-only JSON Lines in `data/audit.log`. Eingaben werden bereinigt (einzeilig, Länge begrenzt, Level geprüft). Über MCP lesbar als Resource `audit://events` |

### Rabattformel

```
1. Mengenstufen (wie Steuerstufen: jede Einheit erhält den Rabatt ihrer Stufe)
      1–9: 0 %   10–49: 5 %   50–99: 10 %   100–249: 15 %   250+: 20 %   → gemischter Satz
2. + Kundenstufe     standard 0 | silver +2 | gold +4 | platinum +6 Prozentpunkte
3. + Bestellwert     +1,5 Pp ab 10.000 €,  +3 Pp ab 50.000 € brutto
4. Kategorie-Deckel  Controllers 18 % | HMI 20 % | Drives 22 % | Safety 15 % | sonst 30 %

Rabatt = min(Mengensatz + Kundenstufe + Bestellwert, Deckel)      Netto = Brutto × (1 − Rabatt)
```

Beispiel: 500 × SAF-RLY01 zu 178 € für einen Platinum-Kunden ergibt 15,94 % + 6 + 3 = 24,94 %. Der Safety-Deckel begrenzt das auf **15 %**, netto **75.650,00 €**. Lagerstand 140, also fehlen 360 Stück.

## LLM-Anbindung

Gesteuert über `LLM_PROVIDER` in der `.env`. Für einen einzelnen Lauf übersteuerbar mit `--provider`.

| `LLM_PROVIDER` | Benötigte Einträge in `.env` | Hinweis |
|---|---|---|
| `openai` | `OPENAI_API_KEY`, `OPENAI_MODEL` | Standard, `gpt-4o-mini` genügt |
| `litellm` | `LITELLM_API_BASE`, `LITELLM_KEY`, `LITELLM_DEFAULT_MODEL` | vorhandener Proxy, oder eigener Proxy (siehe unten) |
| `azure` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` | Azure OpenAI / AI Foundry |
| `ollama` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | lokal, Modell mit Tool-Calling (z. B. `qwen2.5:7b`) |
| `mock` | – | regelbasierter Offline-Ersatz, nur zum Testen der Verkabelung |

**Eigener LiteLLM-Proxy** (falls kein Proxy verfügbar ist): `litellm[proxy]` ist in `requirements.txt` enthalten.
`python start_litellm.py` startet den Proxy auf Port 4000 mit `litellm_config.yaml`, der Proxy leitet an den `OPENAI_API_KEY` weiter.
Voraussetzung: `LITELLM_KEY` und `LITELLM_MASTER_KEY` sind in der `.env` gesetzt und identisch. LiteLLM ≥ 1.105 lehnt Standard-Keys wie `sk-1234` ab.
`agent.py` braucht dafür keine Code-Änderung, nur eine andere Adresse.

## Ausführen

```powershell
python agent.py --demo                                    # 3 Demo-Szenarien
python agent.py "Which sensors have more than 500 units in stock?"   # eigene Frage
python agent.py                                           # interaktiv
python agent.py --demo --provider mock                    # ohne API-Key (Mock-LLM)

# MCP-Server als eigener Prozess über HTTP (entkoppelt, wie Sample 4)
python server.py --http --port 8000                                       # Terminal 1
python agent.py --transport http --url http://127.0.0.1:8000/mcp --demo   # Terminal 2

# MCP-Server als Docker-Container
docker build -t mci-inventory-mcp .
docker run --rm -p 8000:8000 mci-inventory-mcp

python -m pytest -q                                       # 11 Tests, Tools über echten MCP-Client
```

### Demo-Szenarien (`--demo`)

1. **Lagerabfrage und Audit:** findet alle Produkte unter Mindestbestand und schreibt einen `LOW_STOCK_ALERT`.
2. **Angebot über MCP-Prompt:** Der Agent holt die Vorlage `sales_quote_workflow` vom Server (MCP-*Prompt*) und durchläuft SKU-Suche, Rabatt und `QUOTE_CREATED`.
3. **Sonderfall:** 500 Safety Relays für einen Platinum-Kunden lösen den Kategorie-Deckel und eine Fehlmenge aus.

Zum Schluss liest der Agent das Audit-Log über die MCP-*Resource* `audit://events/5` zurück.

## Logs

| Datei | Inhalt |
|---|---|
| `logs/agent_run_<zeit>.log` | ganze ReAct-Spur eines Laufs: `[DISCOVERY]`, `[USER]`, `[ACTION]`, `[OBSERVATION]`, `[FINAL]`, `[RESOURCE]` |
| `logs/sample_execution_openai.log` | Nachweis-Lauf mit OpenAI gpt-4o-mini (im Repo enthalten) |
| `logs/server.log` | jeder Tool-Aufruf auf Server-Seite |
| `data/audit.log` | Audit-Einträge von `append_audit_log` (JSON Lines) |

Auszug aus dem echten Lauf (Szenario 3, gekürzt):

```
[DISCOVERY] tools:   ['query_inventory', 'calculate_tiered_discount', 'append_audit_log']
[USER] A platinum customer wants 500 units of SAF-RLY01. What discount do they get, ...
[ACTION] query_inventory({"sku": "SAF-RLY01"})
[ACTION] calculate_tiered_discount({"quantity": 500, "sku": "SAF-RLY01", "customer_tier": "platinum"})
[ACTION] append_audit_log({"event_type": "QUOTE_CREATED", ...})
[ACTION] append_audit_log({"event_type": "LOW_STOCK_ALERT", "message": "Insufficient stock for SAF-RLY01. Shortfall of 360 units.", ...})
[FINAL] Final Answer: The platinum customer receives a 15% discount, resulting in a net total of 75,650.00 EUR. ...
```

## Projektstruktur

```
agent.py             ReAct-Agent, MCP-Client (stdio / HTTP), Logging, Demo-Szenarien
llm_provider.py      wählbares LLM-Backend (openai | litellm | azure | ollama | mock)
server.py            FastMCP-Server: 3 Tools, 3 Resources, 1 Prompt
inventory_db.py      SQLite-Schema, Beispieldaten, parametrisierte Queries
discount_engine.py   Formel-Engine für Staffelrabatte
audit_log.py         Append-only Audit-Log (JSON Lines)
start_litellm.py     startet den eigenen LiteLLM-Proxy mit den Keys aus .env
litellm_config.yaml  Konfiguration des eigenen LiteLLM-Proxys
tests/test_tools.py  Unit- und MCP-End-to-End-Tests
Dockerfile           MCP-Server als Container (HTTP)
SETUP.md             Installationsanleitung
```

## Designentscheidungen

- **Entkopplung (M×N-Problem):** Der Agent enthält keinen Tool-Code. Die Tools werden zur Laufzeit per `list_tools()` entdeckt und in OpenAI-Function-Schemas übersetzt. Ein neues Tool am Server steht jedem Agent ohne Änderung zur Verfügung.
- **Sicherheit:** parametrisiertes SQL mit Whitelist für die Sortierspalte. Datenbank und Dateipfade bleiben auf der Server-Seite. Audit-Eingaben werden bereinigt und in der Länge begrenzt. Das Audit-Log ist append-only und nur lesend als Resource freigegeben. API-Keys stehen nur in der `.env`, die nicht im Repo liegt.
- **Robuste ReAct-Schleife:** Tool-Fehler (unbekannte SKU, ungültige Kundenstufe) gehen als Observation an das LLM zurück, damit es sich korrigieren kann. `MAX_REACT_STEPS` verhindert Endlosschleifen.
- **Deterministische Zahlen:** Preise, Lagerstände und Rabatte kommen ausschließlich aus den Tools. Das LLM entscheidet nur, *welches* Tool aufgerufen wird.
- **Kompatibilität:** getestet mit FastMCP 3.4.4 / MCP SDK 1.30 (Vorlesungsversion) und FastMCP 4.1 / MCP SDK 2.3.
