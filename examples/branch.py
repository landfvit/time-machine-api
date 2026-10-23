"""Create an inventory history and compare an alternative delivery."""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


def request(base_url, method, path, body=None):
    headers = {"Accept": "application/json"}
    if os.environ.get("TIMEMACHINE_API_KEY"):
        headers["X-API-Key"] = os.environ["TIMEMACHINE_API_KEY"]
    data = None
    if body is not None:
        data = json.dumps(body, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = str(uuid4())
    req = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    original = request(args.url, "POST", "/v1/examples/inventory", {})
    #change the delivery and replay the original builds on a new timeline
    experiment = request(args.url, "POST", f"/v1/timelines/{original['id']}/experiment", {
        "name": "Larger frame delivery",
        "event_version": 1,
        "label": "Twenty frames delivered",
        "operations": [{"op": "increment", "path": "/stock/frames", "value": 20}],
    })
    branch = experiment["timeline"]
    query = urlencode({"left": original["id"], "right": branch["id"]})
    comparison = request(args.url, "GET", f"/v1/compare?{query}")
    print(json.dumps({
        "original_id": original["id"], "branch_id": branch["id"],
        "original_state": comparison["left"]["state"],
        "branch_state": comparison["right"]["state"],
        "changes": comparison["changes"],
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print(f"API returned {error.code}: {error.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except URLError:
        print("Start the local API before running this example.", file=sys.stderr)
        sys.exit(1)
