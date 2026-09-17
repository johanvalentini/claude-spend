"""Parse OTLP/HTTP JSON payloads (logs + metrics) into plain dicts."""

from __future__ import annotations

from typing import Any, Iterator


def any_value(v: dict[str, Any] | None) -> Any:
    """Decode an OTLP AnyValue. int64 may arrive as string or number."""
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return attrs_to_dict(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def attrs_to_dict(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {a["key"]: any_value(a.get("value")) for a in (attrs or []) if "key" in a}


def nano_to_seconds(n: Any) -> float | None:
    if n in (None, "", 0, "0"):
        return None
    return int(n) / 1e9


def iter_log_records(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield {resource, attrs, body, ts, observed_ts, severity} for every log record."""
    for rl in payload.get("resourceLogs", []):
        resource = attrs_to_dict(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                body = any_value(rec.get("body"))
                attrs = attrs_to_dict(rec.get("attributes"))
                ts = nano_to_seconds(rec.get("timeUnixNano")) or nano_to_seconds(
                    rec.get("observedTimeUnixNano")
                )
                yield {
                    "resource": resource,
                    "attrs": attrs,
                    "body": body,
                    "ts": ts,
                    "severity": rec.get("severityText"),
                }


def event_name(record: dict[str, Any]) -> str | None:
    """Return the fully qualified event name, e.g. "claude_code.api_request".

    Claude Code sets the log body to "claude_code.<name>" and the `event.name`
    attribute to the bare "<name>"; accept either and normalise to the prefixed form.
    """
    body = record["body"]
    if isinstance(body, str) and body.startswith("claude_code."):
        return body
    name = record["attrs"].get("event.name")
    if name:
        name = str(name)
        return name if name.startswith("claude_code.") else f"claude_code.{name}"
    return None


def iter_metric_points(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield {resource, name, unit, attrs, value, ts, start_ts, temporality} for Sum/Gauge points."""
    for rm in payload.get("resourceMetrics", []):
        resource = attrs_to_dict(rm.get("resource", {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name")
                unit = metric.get("unit")
                if "sum" in metric:
                    body = metric["sum"]
                    temporality = body.get("aggregationTemporality")
                elif "gauge" in metric:
                    body = metric["gauge"]
                    temporality = None
                else:
                    continue  # histograms etc. not needed
                for dp in body.get("dataPoints", []):
                    if "asDouble" in dp:
                        value = float(dp["asDouble"])
                    elif "asInt" in dp:
                        value = float(int(dp["asInt"]))
                    else:
                        continue
                    yield {
                        "resource": resource,
                        "name": name,
                        "unit": unit,
                        "attrs": attrs_to_dict(dp.get("attributes")),
                        "value": value,
                        "ts": nano_to_seconds(dp.get("timeUnixNano")),
                        "start_ts": nano_to_seconds(dp.get("startTimeUnixNano")),
                        "temporality": temporality,
                    }
