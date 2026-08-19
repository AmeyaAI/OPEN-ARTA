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


# A realistic (invented) deep introspection: query root with a list-of-object
# field, a required-arg field, and a scalar field; an object type with scalars +
# a nested object; a mutation (must never be emitted as a read).
_DEEP_SCHEMA = {"data": {"__schema": {
    "queryType": {"name": "Query"},
    "mutationType": {"name": "Mutation"},
    "subscriptionType": None,
    "types": [
        {"name": "Query", "kind": "OBJECT", "fields": [
            {"name": "products", "args": [],
             "type": {"kind": "LIST", "name": None,
                      "ofType": {"kind": "OBJECT", "name": "Product"}}},
            {"name": "product", "args": [
                {"name": "id", "type": {"kind": "NON_NULL", "name": None,
                                        "ofType": {"kind": "SCALAR", "name": "ID"}}}],
             "type": {"kind": "OBJECT", "name": "Product"}},
            {"name": "health", "args": [], "type": {"kind": "SCALAR", "name": "String"}},
        ]},
        {"name": "Mutation", "kind": "OBJECT", "fields": [
            {"name": "addProduct", "args": [], "type": {"kind": "OBJECT", "name": "Product"}}]},
        {"name": "Product", "kind": "OBJECT", "fields": [
            {"name": "id", "type": {"kind": "SCALAR", "name": "ID"}},
            {"name": "name", "type": {"kind": "SCALAR", "name": "String"}},
            {"name": "supplier", "type": {"kind": "OBJECT", "name": "Supplier"}}]},
        {"name": "Supplier", "kind": "OBJECT", "fields": [
            {"name": "id", "type": {"kind": "SCALAR", "name": "ID"}}]},
    ]}}}


def test_gql_unwrap_type():
    assert pd._gql_unwrap_type({"kind": "SCALAR", "name": "ID"}) == ("SCALAR", "ID")
    # NON_NULL(LIST(OBJECT Product)) → the innermost named type
    assert pd._gql_unwrap_type({"kind": "NON_NULL", "name": None, "ofType": {
        "kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "Product"}}}) \
        == ("OBJECT", "Product")


def test_parse_graphql_read_operations():
    s = pd.parse_graphql_introspection(_DEEP_SCHEMA)
    by = {o["name"]: o for o in s["read_operations"]}
    assert set(by) == {"products", "product", "health"}          # query root only
    # list-of-object → selection of the object's scalar fields (supplier omitted)
    assert by["products"]["query"] == "query { products { id name } }"
    assert by["products"]["requires_args"] is False
    # required (NON_NULL) arg flagged
    assert by["product"]["requires_args"] is True
    # scalar return → leaf query, no selection set
    assert by["health"]["query"] == "query { health }"


def test_build_graphql_read_items_skips_required_and_mutations():
    s = pd.parse_graphql_introspection(_DEEP_SCHEMA)
    items = pd.build_graphql_read_items(s["read_operations"], "http://sut/graphql")
    names = {i["name"] for i in items}
    assert names == {"GraphQL query products", "GraphQL query health"}  # not product (req arg)
    assert all(i["request"]["method"] == "POST" for i in items)
    body = next(i for i in items if "products" in i["name"])["request"]["body"]["raw"]
    assert '"query": "query { products { id name } }"' in body
    # mutation addProduct is never emitted as a read
    assert not any("addProduct" in n for n in names)
    assert pd.build_graphql_read_items([], "http://x") == []


def test_build_protocol_nodes_enriches_query_nodes():
    s = pd.parse_graphql_introspection(_DEEP_SCHEMA)
    g = pd.build_protocol_nodes(graphql=s, graphql_endpoint="http://sut/graphql")
    qn = {n["name"]: n for n in g["nodes"] if n.get("kind") == "graphql_operation"}
    assert qn["products"]["query"] == "query { products { id name } }"
    assert qn["products"]["endpoint"] == "http://sut/graphql"
    assert qn["product"]["requires_args"] is True
    assert qn["addProduct"].get("query") is None      # mutation carries no read query


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
