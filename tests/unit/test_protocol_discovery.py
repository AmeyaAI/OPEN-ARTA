"""Phase 5 — multi-protocol discovery parsers (GraphQL / gRPC / event-driven)."""
from __future__ import annotations

import src.agents.protocol_discovery as pd


def test_parse_graphql_introspection():
    data = {"data": {"__schema": {
        "queryType": {"name": "Query"},
        "mutationType": {"name": "Mutation"},
        "subscriptionType": None,
        "types": [
            {"name": "Query", "kind": "OBJECT", "fields": [{"name": "datasets"}, {"name": "user"}]},
            {"name": "Mutation", "kind": "OBJECT", "fields": [{"name": "createDataset"}]},
            {"name": "Dataset", "kind": "OBJECT", "fields": []},
            {"name": "__Type", "kind": "OBJECT", "fields": []},
        ]}}}
    s = pd.parse_graphql_introspection(data)
    assert s["queries"] == ["datasets", "user"]
    assert s["mutations"] == ["createDataset"]
    assert s["subscriptions"] == []
    assert "Dataset" in s["types"] and "__Type" not in s["types"]


def test_parse_proto():
    proto = """
    syntax = "proto3";
    service DatasetService {
      rpc CreateDataset (CreateRequest) returns (Dataset);
      rpc StreamRows (RowQuery) returns (stream Row);
    }
    message CreateRequest { string name = 1; }
    message Dataset { string id = 1; }
    message Row { }
    """
    p = pd.parse_proto(proto)
    svc = next(s for s in p["services"] if s["name"] == "DatasetService")
    methods = {m["name"]: m for m in svc["methods"]}
    assert methods["CreateDataset"]["request"] == "CreateRequest"
    assert methods["CreateDataset"]["response"] == "Dataset"
    assert methods["StreamRows"]["streaming"] is True
    assert "CreateRequest" in p["messages"] and "Row" in p["messages"]


def test_detect_event_channels_kafka():
    src = """
    from kafka import KafkaConsumer, KafkaProducer
    consumer = KafkaConsumer('ingest-events')
    producer.send('processed-events', value=b'x')
    """
    chans = pd.detect_event_channels(src)
    systems = {(c["system"], c["channel"]) for c in chans}
    assert ("kafka", "ingest-events") in systems
    assert ("kafka", "processed-events") in systems


def test_detect_event_channels_rabbitmq():
    src = """
    import pika
    channel.queue_declare(queue='task_queue')
    channel.basic_publish(exchange='', routing_key='task_queue', body=msg)
    """
    chans = pd.detect_event_channels(src)
    assert any(c["system"] == "rabbitmq" and c["channel"] == "task_queue" for c in chans)


def test_detect_event_channels_empty_without_lib():
    assert pd.detect_event_channels("def f(): return 1") == []


def test_build_protocol_nodes():
    g = pd.build_protocol_nodes(
        graphql={"queries": ["datasets"], "mutations": ["createDataset"], "subscriptions": []},
        proto={"services": [{"name": "Svc", "methods": [
            {"name": "M", "request": "Req", "response": "Resp", "streaming": False}]}]},
        event_channels=[{"system": "kafka", "role": "consumer", "channel": "ingest"}])
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"graphql_operation", "grpc_method", "event_channel"} <= kinds
    assert g["protocol_counts"]["graphql"] == 2
    assert g["protocol_counts"]["grpc"] == 1
    assert g["protocol_counts"]["event"] == 1
