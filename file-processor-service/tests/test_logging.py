import json
import logging

from app.logging_config import JsonFormatter, bind, log_context, step, trace_field


def capture(logger_name="t"):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    lg = logging.getLogger(logger_name)
    lg.handlers[:] = [handler]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return lg, records


def test_context_is_added_to_every_line_and_restored():
    lg, records = capture()
    fmt = JsonFormatter()
    with log_context(message_id="m1", **trace_field("abc123/1;o=1", "proj")):
        bind(job_id="j1")
        lg.info("inside", extra={"rows": 3})
    lg.info("outside")

    inside, outside = (json.loads(fmt.format(r)) for r in records)
    assert inside["message"] == "inside"
    assert inside["severity"] == "INFO"
    assert inside["message_id"] == "m1" and inside["job_id"] == "j1" and inside["rows"] == 3
    assert inside["logging.googleapis.com/trace"] == "projects/proj/traces/abc123"
    assert "job_id" not in outside and "message_id" not in outside


def test_step_logs_duration_results_and_caller_location():
    lg, records = capture()
    with step(lg, "download", uri="gs://b/f") as s:
        s["bytes"] = 10
    done = json.loads(JsonFormatter().format(records[-1]))
    assert done["message"].startswith("download done in ")
    assert done["message"].endswith("uri=gs://b/f bytes=10")
    assert done["step"] == "download" and done["bytes"] == 10 and "duration_ms" in done
    assert (
        done["logging.googleapis.com/sourceLocation"]["function"]
        == "test_step_logs_duration_results_and_caller_location"
    )


def test_trace_field_without_header():
    assert trace_field(None, "proj") == {}
