# 🔐 Automated Incident Management Engine

Custom Python automation engine designed to manage infrastructure health alerts by integrating:

- Elasticsearch (event source)
- SOAR platform (SimpleClient)
- External Ticketing API
- Parallel execution with threading

This script replaces multiple cron-based workflows with a unified, thread-safe automation engine.

---

## 📌 Overview

This engine automatically manages two main flows:

### 🟥 Degraded Flow (Health = DOWN / CRITICAL / WARNING / UNKNOWN)

1. Searches for degraded infrastructure records.
2. Checks if a master incident already exists.
3. If no active master:
   - Creates an external ticket.
   - Escalates the incident in the SOAR platform.
4. If a master is already escalated:
   - Prevents duplicate escalation.

---

### 🟢 Operational Flow (Health = UP / OK)

1. Searches for operational records.
2. Checks for an existing escalated master.
3. If found:
   - Closes the master incident automatically.

---

## 🏗 Architecture

Elasticsearch (event_records_index)  
        ↓  
fetch_all_degraded_records()  
fetch_all_operational_records()  
        ↓  
Master lookup (find_master_record)  
        ↓  
Ticket API  ←→  SOAR Platform  

The engine runs:

- process_degraded_flow() → Thread 1
- process_operational_flow() → Thread 2
- Coordinated via run_parallel_flows()

This prevents:

- Race conditions
- Double ticket creation
- Cron execution conflicts
- API collisions

---

## ⚙️ Configuration

The script requires a `settings.ini` file:

```ini
[PLATFORM]
url=https://your-soar-instance
user=api_user
password=api_password
org=your_org

[TICKETING]
api_key=xxxxxxxx
base_url=https://your-ticketing-api

[SEARCH]
host=localhost
user=elastic
password=changeme
