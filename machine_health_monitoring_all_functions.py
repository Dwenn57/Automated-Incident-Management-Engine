from elasticsearch import Elasticsearch
import requests
import json
import logging
from soar import SimpleClient
import configparser
import sys
import time
from datetime import datetime, timedelta
import subprocess
from jinja2 import Environment, FileSystemLoader
import threading


current_day = datetime.now().date()

config = configparser.ConfigParser()
config.read("settings.ini")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

app_config = {
    "platform_url": config["PLATFORM"]["url"],
    "platform_user": config["PLATFORM"]["user"],
    "platform_password": config["PLATFORM"]["password"],
    "platform_org": config["PLATFORM"]["org"],
    "ticket_api_key": config["TICKETING"]["api_key"],
    "ticket_base_url": config["TICKETING"]["base_url"]
}

search_client = Elasticsearch(
    hosts=f"http://{config['SEARCH']['host']}:9200",
    http_auth=[
        config["SEARCH"]["user"],
        config["SEARCH"]["password"]
    ]
)

platform_client = SimpleClient(
    org_name=app_config["platform_org"],
    base_url=app_config["platform_url"],
    verify=False
)


# ==========================================================
# FIELD VALUE MAPPING
# ==========================================================

def build_field_value_maps(app_config, platform_client):

    state_map = {}
    priority_map = {}
    category_map = {}
    requester_reply_map = {}
    internal_flag_map = {}
    relation_reason_map = {}
    master_flag_map = {}

    platform_client.connect(
        app_config["platform_user"],
        app_config["platform_password"]
    )

    field_state = platform_client.get("/types/incident/fields/state_code")
    field_priority = platform_client.get("/types/incident/fields/priority_code")
    field_category = platform_client.get("/types/incident/fields/category_code")
    field_requester = platform_client.get("/types/incident/fields/requester_reply")
    field_internal = platform_client.get("/types/incident/fields/internal_flag")
    field_relation = platform_client.get("/types/incident/fields/relation_reason")
    field_master = platform_client.get("/types/incident/fields/master_flag")

    for v in field_state["values"]:
        state_map[v["label"]] = v["value"]

    for v in field_priority["values"]:
        priority_map[v["label"]] = v["value"]

    for v in field_category["values"]:
        category_map[v["label"]] = v["value"]

    for v in field_requester["values"]:
        requester_reply_map[v["label"]] = v["value"]

    for v in field_internal["values"]:
        internal_flag_map[v["label"]] = v["value"]

    for v in field_relation["values"]:
        relation_reason_map[v["label"]] = v["value"]

    for v in field_master["values"]:
        master_flag_map[v["label"]] = v["value"]

    return {
        "state": state_map,
        "priority": priority_map,
        "category": category_map,
        "requester_reply": requester_reply_map,
        "internal_flag": internal_flag_map,
        "relation_reason": relation_reason_map,
        "master_flag": master_flag_map
    }


# ==========================================================
# ESCALATION UPDATE
# ==========================================================

def escalate_existing_record(record, app_config, platform_client, external_ticket_id):

    record_id = record["_id"]

    platform_client.connect(
        app_config["platform_user"],
        app_config["platform_password"]
    )

    incident = platform_client.get(f"/incidents/{record_id}")
    value_maps = build_field_value_maps(app_config, platform_client)

    note = f"{record['_source']['summary_text']} - Investigation required."

    incident["properties"]["state_code"] = value_maps["state"]["Escalated"]
    incident["properties"]["priority_code"] = value_maps["priority"]["High"]
    incident["properties"]["master_flag"] = value_maps["master_flag"]["YES"]
    incident["properties"]["analysis_note"] = note
    incident["properties"]["closure_note"] = note
    incident["properties"]["external_reference"] = external_ticket_id
    incident["properties"]["display_name"] = f"AUTO-{record['_source']['tenant_name']}-{record_id}"

    platform_client.put(f"/incidents/{record_id}", incident)


# ==========================================================
# CREATE TICKET
# ==========================================================

def create_external_ticket(app_config, record):

    session = requests.Session()

    ticket_priority = "High"
    record_id = record["_id"]

    tenant_name = "GENERIC_ACCOUNT"
    support_team = "OPERATIONS_TEAM"

    creation_timestamp = str(datetime.now())
    title = f"AUTO-{tenant_name}-{record_id}"

    description = record["_source"]["summary_text"]
    service_health = record["_source"]["service_health"]

    template_loader = FileSystemLoader("templates")
    env = Environment(loader=template_loader)
    template = env.get_template("ticket_template.html")

    rendered_content = template.render(
        title=title,
        description=description,
        health=service_health,
        priority=ticket_priority
    )

    subject = f"{tenant_name} - {record['_source']['component_name']} - {service_health}"

    endpoint = app_config["ticket_base_url"] + "/requests"

    payload = {
        "request": {
            "subject": subject,
            "description": rendered_content,
            "priority": {"name": ticket_priority},
            "group": {"name": support_team},
            "status": {"name": "Open"},
            "custom_attributes": {
                "record_identifier": record_id,
                "creation_time": creation_timestamp
            }
        }
    }

    params = {
        "TECHNICIAN_KEY": app_config["ticket_api_key"],
        "format": "json",
        "input_data": json.dumps(payload)
    }

    response = session.post(endpoint, verify=False, params=params)
    response_json = json.loads(response.content)

    return {
        "external_ticket_id": response_json["request"]["id"],
        "ticket_priority": ticket_priority
    }


# ==========================================================
# SEARCH MASTER RECORD
# ==========================================================

def find_master_record(node_name, component_name):

    query = {
        "query": {
            "bool": {
                "must": [
                    {"match": {"node.keyword": node_name}},
                    {"match": {"component_name": component_name}},
                    {"match": {"state_code": "Escalated"}},
                    {"match": {"master_flag": "YES"}},
                ],
                "filter": [
                    {"range": {"event_time": {"gte": "now-30d", "lte": "now"}}}
                ]
            }
        },
        "sort": [{"event_time": {"order": "asc"}}]
    }

    response = search_client.search(
        index="event_records_index",
        body=query,
        size=10
    )

    return response["hits"]["hits"][0] if response["hits"]["hits"] else []


# ==========================================================
# CLOSE MASTER RECORD
# ==========================================================

def close_master_record(trigger_record, master_record, app_config, platform_client):

    value_maps = build_field_value_maps(app_config, platform_client)

    master_id = master_record["_id"]

    platform_client.connect(
        app_config["platform_user"],
        app_config["platform_password"]
    )

    incident = platform_client.get(f"/incidents/{master_id}")

    resolution_note = f"{trigger_record['_source']['summary_text']} - Resolved."

    incident["properties"]["state_code"] = value_maps["state"]["Closed"]
    incident["properties"]["closure_note"] = resolution_note
    incident["properties"]["master_flag"] = value_maps["master_flag"]["YES"]

    platform_client.put(f"/incidents/{master_id}", incident)


# ==========================================================
# FETCH MACHINES WITH BAD HEALTH
# ==========================================================

def fetch_all_degraded_records():

    logger = logging.getLogger(__name__)

    query = {
        "_source": [],
        "query": {
            "bool": {
                "must": [
                    {"match": {"event_type": "Host Alert"}},
                    {"match": {"state_code": "Open"}},
                    {"match": {"tenant_name": "GENERIC_ACCOUNT"}},

                    {
                        "bool": {
                            "should": [
                                {
                                    "wildcard": {
                                        "node": {
                                            "value": "nodeA*",
                                            "case_insensitive": "true"
                                        }
                                    }
                                },
                                {
                                    "wildcard": {
                                        "node": {
                                            "value": "node*",
                                            "case_insensitive": "true"
                                        }
                                    }
                                }
                            ],
                            "minimum_should_match": 1
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"match": {"service_health.keyword": "DOWN"}},
                                {"match": {"service_health.keyword": "CRITICAL"}},
                                {"match": {"service_health.keyword": "WARNING"}},
                                {"match": {"service_health": "UNKNOWN"}}
                            ],
                            "minimum_should_match": 1
                        }
                    }
                ],
                "must_not": [
                    {"match": {"master_flag": "YES"}},
                    {"match": {"service_health.keyword": "OK"}},
                    {"match": {"service_health.keyword": "UP"}},
                    {
                        "wildcard": {
                            "node": {
                                "value": "excluded*",
                                "case_insensitive": "true"
                            }
                        }
                    },
                    {"match": {"node.keyword": "excluded.node.local"}}
                ],
                "filter": [
                    {
                        "range": {
                            "event_time": {
                                "gte": "now-24h",
                                "lte": "now"
                            }
                        }
                    }
                ]
            }
        },
        "sort": [
            {
                "event_time": {"order": "asc"}
            }
        ]
    }

    response = search_client.search(
        index="event_records_index",
        body=query,
        size=10
    )

    results = response["hits"]["hits"]

    logger.info(f"number of degraded records: {len(results)}")

    for r in results:
        logger.info(r["_source"]["service_health"])

    return results



# ==========================================================
# FETCH MACHINES IN GOOD HEALTH
# ==========================================================

def fetch_all_operational_records():

    logger = logging.getLogger(__name__)

    query = {
        "_source": [],
        "query": {
            "bool": {
                "must": [
                    {"match": {"tenant_name": "GENERIC_ACCOUNT"}},
                    {"match": {"state_code": "Open"}}
                ],
                "should": [
                    {
                        "wildcard": {
                            "node": {
                                "value": "nodeA*",
                                "case_insensitive": "true"
                            }
                        }
                    },
                    {
                        "wildcard": {
                            "node": {
                                "value": "node*",
                                "case_insensitive": "true"
                            }
                        }
                    },
                    {"match": {"service_health.keyword": "UP"}},
                    {"match": {"service_health.keyword": "OK"}}
                ],
                "must_not": [
                    {"match": {"master_flag": "YES"}},
                    {"match": {"service_health.keyword": "DOWN"}},
                    {"match": {"service_health.keyword": "CRITICAL"}},
                    {"match": {"service_health.keyword": "WARNING"}},
                    {"match": {"service_health.keyword": "UNKNOWN"}}
                ],
                "filter": [
                    {
                        "range": {
                            "event_time": {
                                "gte": "now-24h",
                                "lte": "now"
                            }
                        }
                    }
                ]
            }
        },
        "sort": [
            {
                "event_time": {"order": "asc"}
            }
        ]
    }

    response = search_client.search(
        index="event_records_index",
        body=query,
        size=10
    )

    results = response["hits"]["hits"]

    logger.info(f"number of operational records: {len(results)}")

    for r in results:
        logger.info(r["_source"]["service_health"])

    return results




# ==========================================================
# PROCESS MONITOR
# ==========================================================

def check_running_task(task_keyword):

    result = subprocess.run(
        ["ps", "aux"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )

    for line in result.stdout.splitlines():
        if task_keyword.lower() in line.lower() and "grep" not in line:
            return line

    return ""


# ==========================================================
# DEGRADED FLOW (ex DOWN cron)
# ==========================================================

def process_degraded_flow():

    logger.info("Starting degraded flow thread")

    degraded_records = fetch_all_degraded_records()

    if not degraded_records:
        logger.info("No degraded records found")
        return

    oldest_record = degraded_records[0]

    node_name = oldest_record["_source"]["node"]
    component_name = oldest_record["_source"]["component_name"]
    record_time_str = oldest_record["_source"]["event_time"][0:19]
    record_time = datetime.strptime(record_time_str, "%Y-%m-%dT%H:%M:%S")

    master_record = find_master_record(node_name, component_name)

    if master_record:
        logger.info("Master record already exists → escalation logic")

        if master_record["_source"]["state_code"] == "Escalated":
            logger.info("Master already escalated → nothing to create")
            return

    logger.info("No active master → creating external ticket")

    ticket = create_external_ticket(app_config, oldest_record)
    external_ticket_id = ticket["external_ticket_id"]

    escalate_existing_record(
        oldest_record,
        app_config,
        platform_client,
        external_ticket_id
    )

    logger.info("Degraded flow finished")


# ==========================================================
# OPERATIONAL FLOW (ex UP cron)
# ==========================================================

def process_operational_flow():

    logger.info("Starting operational flow thread")

    operational_records = fetch_all_operational_records()

    if not operational_records:
        logger.info("No operational records found")
        return

    for record in operational_records:

        node_name = record["_source"]["node"]
        component_name = record["_source"]["component_name"]

        master_record = find_master_record(node_name, component_name)

        if not master_record:
            continue

        if master_record["_source"]["state_code"] == "Escalated":

            logger.info(
                f"Closing master for {node_name} / {component_name}"
            )

            close_master_record(
                record,
                master_record,
                app_config,
                platform_client
            )

    logger.info("Operational flow finished")


# ==========================================================
# THREAD MANAGER
# ==========================================================

def run_parallel_flows():

    degraded_thread = threading.Thread(
        target=process_degraded_flow,
        name="DegradedThread"
    )

    operational_thread = threading.Thread(
        target=process_operational_flow,
        name="OperationalThread"
    )

    degraded_thread.start()

    # léger décalage pour éviter collision API
    time.sleep(5)

    operational_thread.start()

    degraded_thread.join()
    operational_thread.join()

    logger.info("All flows completed")


# ==========================================================
# MAIN ENTRYPOINT
# ==========================================================

if __name__ == "__main__":

    logger.info("Auto management engine started")

    run_parallel_flows()

    logger.info("Execution finished")
