"""客户历史维护接口。"""

import json

from flask import Blueprint, Response, current_app, jsonify, request

from app.business import DomainIssue
from app.history import service
from app.inputs import read_input

history_api = Blueprint("history_api", __name__, url_prefix="/api/history")


def store():
    return current_app.extensions["history"]


@history_api.post("/preview")
def preview():
    return jsonify(service.preview(store(), read_input()))


@history_api.route("/batches", methods=["GET", "POST"])
def batches():
    if request.method == "GET":
        return jsonify(items=store().list_batches())
    return jsonify(service.submit(store(), read_input()))


@history_api.get("/batches/<batch_id>")
def batch(batch_id):
    return jsonify(service.evaluate(store(), store().load_batch(batch_id)))


@history_api.post("/batches/<batch_id>/resolutions")
def resolve(batch_id):
    return jsonify(
        service.resolve_conflict(store(), batch_id, request.get_json(silent=True))
    )


@history_api.post("/batches/<batch_id>/publish")
def publish(batch_id):
    return jsonify(service.publish(store(), batch_id))


@history_api.get("/current")
def current():
    version = store().current_version()
    if version is None:
        return jsonify(version=None, timeline={})
    return jsonify(version=version["version"], timeline=version["snapshot"]["timeline"])


@history_api.get("/batches/<batch_id>/evidence")
def export(batch_id):
    batch = store().load_batch(batch_id)
    packet = service.evidence(store(), batch)
    return Response(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="history-{batch_id[:12]}.json"',
            "Cache-Control": "no-store",
        },
    )
