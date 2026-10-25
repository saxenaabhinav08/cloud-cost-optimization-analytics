# Cloud Cost Optimization Analytics

A beginner-friendly Python portfolio project for exploring a **fictional AWS billing dataset**. The Python pipeline validates sample data, totals costs by service and month, applies one documented spending flag, and exports Tableau-ready CSVs. Phase 2 adds an optional MySQL 8 schema and importer.

> **Important:** The included rows are invented for development. They are not AWS Cost and Usage Report data, do not represent an AWS account, and do not demonstrate real savings. Nothing in this project connects to AWS or needs AWS credentials. The importer only sends validated fictional CSV rows to a MySQL database that you configure locally. A Tableau dashboard is not part of this version.

## What this project does

1. Checks the input schema, required text, month format, numeric non-negative cost, and explicit fictional-data markers.
2. Aggregates costs to one row for each service and month.
3. Flags large increases with a simple, documented rule.
4. Writes two Tableau-ready CSV files.
5. Optionally imports validated fictional records into MySQL 8 and provides SQL analysis queries.
6. Provides a local Streamlit dashboard for the same fictional sample or fictional records stored in local MySQL.

## Project layout

```text
cloud-cost-optimization-analytics/
├── app.py                             # Streamlit dashboard entry point
├── data/
│   ├── raw/fictional_aws_billing.csv   # Hand-authored fictional input
│   ├── synthetic/daily_cloud_billing.csv # Generated daily synthetic dashboard data
│   └── processed/                     # Generated Tableau CSVs
├── src/cloud_cost_analytics/
│   ├── validation.py                  # Input checks and typed records
│   ├── aggregation.py                 # Service/month totals
│   ├── anomalies.py                   # Spending rule
│   ├── pipeline.py                    # Command-line runner and CSV exports
│   ├── database.py                    # Optional SQLAlchemy/MySQL configuration
│   ├── mysql_import.py                # Validated, idempotent CSV importer
│   ├── dashboard_data.py              # Dashboard data loading and shared analysis
│   ├── finops_analytics.py            # Shared daily-data analytics and forecasts
│   ├── finops_app.py                  # Eight distinct Streamlit destinations
│   ├── synthetic_data.py              # Seeded fictional daily data generator
│   └── streamlit_app.py               # Dashboard entry point
├── sql/
│   ├── schema.sql                     # MySQL 8 table and indexes
│   └── analytics_queries.sql          # Monthly, service, region, and MoM queries
├── tests/                             # Local tests; no MySQL server required
│   ├── test_pipeline.py
│   ├── test_mysql_integration.py
│   ├── test_dashboard_data.py
│   └── test_finops_analytics.py
├── .gitignore
├── .env.example
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Spending anomaly rule

For each service and month, the pipeline computes the average cost across up to the three immediately preceding calendar months that are present. It flags the current month only when both conditions are true:

- Current cost is greater than 1.5 times the baseline.
- Current cost is at least $100 above the baseline.

At least one preceding month must exist. This small, explainable rule is useful for learning and may miss patterns or produce false alarms. It is not a statistical forecast, financial advice, or proof that spend is wasteful. Review any flag in context.

## Windows setup (PowerShell)

Open PowerShell in this project folder. Python 3.10 or newer is required; this project was run with Python 3.13.

```powershell
py -3 --version
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The virtual environment keeps dependencies local to this project. The analytics pipeline itself uses only Python's standard library. No AWS software, credentials, database, or Tableau installation is required to run the Python pipeline or test suite.

If PowerShell blocks activation, use the environment's Python directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run the pipeline

From this project folder, with the environment activated:

```powershell
$env:PYTHONPATH = "src"
python -m cloud_cost_analytics.pipeline
```

The default input is `data/raw/fictional_aws_billing.csv`; exports go into `data/processed/`:

- `service_monthly_costs.csv` — `billing_month`, `service`, and `total_cost_usd`.
- `spending_anomalies.csv` — current value, comparison baseline, increase, applied rule, and review note.

You can pass paths explicitly:

```powershell
python -m cloud_cost_analytics.pipeline --input data/raw/fictional_aws_billing.csv --output-dir data/processed
```

In Tableau, connect to either CSV using **Text File**. Relate or join them using `billing_month` and `service` if both are used in one workbook. The exports contain fictional sample results only.

## Run tests

```powershell
python -m pytest
```

The tests cover fictional-data enforcement, validation of negative amounts, monthly aggregation, the anomaly rule, output file creation, database URL configuration, deterministic import fingerprints, mocked MySQL behavior, and dashboard summaries and data loading. They do not require a MySQL server.

## Phase 2: optional MySQL 8 integration

### Install MySQL separately

The project does not install, start, or configure a database server. If MySQL is not already installed, use the official [MySQL 8 Windows installation instructions](https://dev.mysql.com/doc/refman/8.0/en/windows-installation.html) and [MySQL Installer download page](https://dev.mysql.com/downloads/installer/). Select MySQL Server 8.x and complete its setup in the installer. You may choose to configure it as a Windows service. This server installation is a separate system change; none of the commands below perform it.

### Create a local database and restricted user

After the local server is installed and running, use MySQL Workbench or the MySQL command-line client to connect as an administrator. Run the following SQL there, replacing the example password locally with a strong password. Do not share it in chat or commit it:

```sql
CREATE DATABASE IF NOT EXISTS cloud_cost_analytics
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'cloud_analytics_app'@'localhost' IDENTIFIED BY RANDOM PASSWORD;
```

MySQL prints the generated application password in your local terminal. Save it privately for the `.env` setup below; do not paste it into chat. `IDENTIFIED BY RANDOM PASSWORD` is supported by MySQL 8.0.18 and later. If this account already exists, stop and inspect its grants rather than resetting its password.

Apply the additive schema as the administrator, then grant the application user only the privileges it needs. The importer uses plain inserts and handles a duplicate fingerprint as a no-op, so it does not need `UPDATE`:

```powershell
cmd.exe /c "mysql -u root -p cloud_cost_analytics < sql\schema.sql"
```

Then, in an administrator MySQL session:

```sql
GRANT SELECT, INSERT ON cloud_cost_analytics.* TO 'cloud_analytics_app'@'localhost';
```

`schema.sql` uses `CREATE TABLE IF NOT EXISTS`. It creates the table and indexes only when missing; it does not drop or replace existing data. If you change the schema later, use a reviewed migration rather than rerunning destructive commands.

### Configure the Python importer

Copy the example settings file and edit the local copy. `.env` is ignored by Git:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `DATABASE_HOST=localhost`, `DATABASE_PORT=3306`, `DATABASE_NAME=cloud_cost_analytics`, `DATABASE_USER=cloud_analytics_app`, and `DATABASE_PASSWORD` to the locally generated value. The module also accepts a SQLAlchemy `DATABASE_URL` environment variable. Credentials are only for your local MySQL instance; no AWS credentials are used or supported.

The package dependencies can be installed in the project-local virtual environment using the earlier `python -m pip install -r requirements.txt` command. No system-wide Python packages are installed. `localhost` and port `3306` are the default local connection settings.

### Import the fictional CSV

With MySQL running, its database/table created, `.env` configured, and the virtual environment activated, run:

```powershell
$env:PYTHONPATH = "src"
python -m cloud_cost_analytics.mysql_import
```

The importer validates the **entire CSV before opening a connection** and refuses records not explicitly marked fictional. It uses a SHA-256 fingerprint of the normalized month, account, region, service, usage type, cost, fictional marker, and dataset note as the unique idempotency key. Reimporting an identical row is a no-op. A changed row has a different fingerprint and is inserted as a new record; it does not silently update or delete the older version. Exact duplicate source rows collapse to one database row. Plain `INSERT` statements run inside savepoints; only MySQL duplicate-key error 1062 is skipped, while other database errors abort the import transaction. The importer therefore requires only `INSERT` for loading and `SELECT` for verification/analysis.

### Included SQL analyses

`sql/analytics_queries.sql` contains runnable MySQL 8 queries for total spending by month, spending by AWS service and month, spending by region and month, and month-over-month total change (using `LAG`). Their outputs are fictional sample analysis only.

## Streamlit dashboard

The dashboard offers two local data modes: the included fictional CSV (works offline and needs no AWS credentials) and records marked `is_fictional` in the configured local MySQL database. Both modes use the project's existing input validation, service/month aggregation, and anomaly rule. A missing database, schema, or connection is reported in the dashboard without showing credentials or raw database error details. MySQL mode is not a live AWS integration. All included data remains fictional; no actual AWS costs or savings are represented.

### Phase 1 FinOps analytics upgrade

The expanded dashboard adds a separate, reproducible daily dataset at `data/synthetic/daily_cloud_billing.csv`. It contains **16,425 synthetic records** covering every day in 2025, 3 fictional account IDs, 12 service categories, 4 regions, and 15 resource templates per account/day. Team, project, and environment tags are sometimes intentionally blank; the app keeps these costs unassigned and reports incomplete allocation. Injected scenarios include a simulated runaway NAT Gateway batch (synthetic-002, August), a cross-region data backfill (synthetic-001, April), and an oversized development EC2 rollout (synthetic-003, November). These are training examples, not observations of AWS.

Regenerate the dataset with the fixed default seed or choose another seed explicitly:

```powershell
$env:PYTHONPATH = "src"
python -m cloud_cost_analytics.synthetic_data
python -m cloud_cost_analytics.synthetic_data --seed 73
```

The dashboard uses this daily dataset for **Fictional Sample** mode, generating it on first launch if the file is missing. The original `data/raw/fictional_aws_billing.csv` (18 monthly records) remains unchanged for backward-compatible pipeline and MySQL importer exercises. The MySQL schema/importer are unchanged in this phase: their monthly row format does not contain resource IDs, daily dates, or allocation tags, so those fields are not fabricated in MySQL mode and daily analyses may be limited.

The eight navigation destinations have separate content: Overview (headline metrics, ranked service spend, and evidence-based notes); Cost Trends (daily/monthly views and rolling average); Service Breakdown (dimension selector and allocation gaps); Anomalies (28-observation rolling baseline, statuses saved in the current browser session only); Optimization (simulated idle/unattached resource estimates); Budgets & Forecasts (illustrative team budgets and straight-line projection); Ask AI (read-only SQL answers over the selected rows); and Billing Records (search, pagination, CSV download).

### Ask AI safety and query coverage

Ask AI works offline with twelve fixed, parameterized SQLite queries: top service, monthly change, service movers, untagged spend, largest anomaly, simulated savings opportunities, illustrative budget risk, spend by region, spend by environment, month-end run rate, simulated idle resources, and week-over-week movement. It also accepts a follow-up such as “break that down by team.” Replies include the result table and a **How I got this** panel with the query and bound parameters. Unsupported questions receive suggestions instead of invented results. The selected frame and derived local summaries are copied into a temporary in-memory SQLite database opened in query-only mode; the app does not modify MySQL.

Optional OpenAI SQL generation is guarded by two explicit settings so that merely storing an API key never sends a request. Put `OPENAI_API_KEY` and `ASK_AI_LLM_ENABLED=true` in the ignored local `.env` file (or Streamlit secrets), then check the in-app **Use configured AI query generation** box before sending a question. API calls may incur charges. Only the question and documented table schema are sent; billing rows remain local. Every returned query is checked with SQLGlot for one SELECT statement, allowed tables and columns, no comments, and a maximum 500-row limit before it is run against the query-only in-memory database. Generated query time is bounded. The default suite and demo do not need a key or external model access.

The shared USD formatter shows whole dollars in dashboard metrics, summaries, and charts, compact K/M values on the ranked chart and donut centre, and cents only in the detailed Billing Records table. Overview service bars compare the latest two represented billing months per service; the fictional generator has distinct service growth and seasonality profiles to make the comparison varied. This synthetic variation is for testing only, not a forecast of real AWS behavior.

Forecasting is straight-line: month-to-date daily average multiplied by calendar days in month. It does not account for seasonality or provide confidence intervals. The anomaly rule compares daily account/service/region cost to the preceding 28 observations (minimum 14), flagging values above 2.5× baseline and at least $10 higher. It is not seasonality-aware. Potential savings use mean simulated daily charges × 30 and are estimates only. Budget amounts are illustrative planning assumptions. Nothing in the app shuts down resources, changes AWS, or establishes real savings.

### Run locally on Windows (PowerShell)

From the project folder, install dependencies into the project-local virtual environment and launch Streamlit:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

If `.venv` already exists, skip the `venv` creation command. The dashboard opens in your local browser. Choose **Fictional sample** to start without MySQL. To use **Local MySQL**, start your separately installed local server, apply `sql/schema.sql` to the configured database, and use the ignored project `.env` settings described above. The dashboard only reads fictional rows and does not create tables or modify database records.

To run using an already activated environment instead, set `$env:PYTHONPATH = "src"` and run `python -m streamlit run app.py`.

### Visual QA screenshots (optional)

The checked-in scripts/screenshot.py uses headless Playwright to capture all eight dashboard pages, Overview composition states, Service Breakdown tabs, and three Ask AI states (suggested question, typed question, and unsupported question) at a 1440 × 900 browser viewport. It expands Streamlit's internal scroll area for full-page PNGs. In PowerShell, start the app in one terminal:

~~~powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.address 127.0.0.1 --server.port 8501
~~~

In a second terminal, install Chromium once and run the capture:

~~~powershell
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts\screenshot.py
~~~

PNG files are saved in screenshots\. This is local-only visual verification; it does not connect to AWS or publish the dashboard.

## Current boundaries

The project includes optional local MySQL storage and a local Streamlit dashboard. It does not connect to AWS, create cloud resources, use AWS credentials, or include a Tableau dashboard. The default test suite does not need a MySQL server; database access tests mock SQLAlchemy connections. The generated dataset is entirely synthetic, and its simulations do not validate cloud savings.

## CSV input fields

| Field | Meaning |
|---|---|
| `billing_month` | Month formatted as `YYYY-MM` |
| `account_id` | Fake account identifier; not a real account |
| `region` | Fictional sample region label |
| `service` | Service name used for grouping |
| `usage_type` | Broad sample usage category |
| `cost_usd` | Fictional non-negative amount in USD |
| `is_fictional` | Must be `true` for this development-only pipeline |
| `dataset_note` | Must explicitly describe fictional data |

## Scope and next steps

Possible later portfolio steps include connecting to a real AWS billing export with explicit credentials and access controls, and building a Tableau dashboard. They are not implemented here.

## Phase 2A: AWS ingestion preparation (offline only)

This phase adds optional, disabled-by-default Cost Explorer and Athena CUR ingestion modules, canonical Decimal records, normalization, bounded request/query clients, an additive MySQL migration, transactional restatement/audit support, and mocked offline tests. It does not call AWS, read real billing data, alter the existing dashboard or Ask AI allowlist, or establish savings. It has **not been verified against a real AWS account**.

The optional AWS client extra can be installed in the project environment with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[aws]"
```

Installing it does not enable ingestion. The default `AWS_INGESTION_ENABLED` value is false. See [Phase 2A design and safety notes](docs/aws_ingestion_phase_2a.md) for the data flow, configuration reference, cost considerations, and future least-privilege review. No AWS credentials are requested or stored for this phase.
