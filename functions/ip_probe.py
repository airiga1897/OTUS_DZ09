"""Return the source address supplied by the YC HTTPS router."""
from ipaddress import IPv4Address
import json
import re


def handler(event, context):
    nonce = (event.get("queryStringParameters") or {}).get("nonce", "")
    try:
        if not re.fullmatch(r"[a-f0-9]{32}", nonce):
            raise ValueError("Invalid nonce")
        address = IPv4Address(event["requestContext"]["identity"]["sourceIp"])
        if not address.is_global or address.is_multicast:
            raise ValueError("Public IPv4 required")
    except (KeyError, TypeError, ValueError):
        return {"statusCode": 400, "body": "Public IPv4 and nonce required"}
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps({"ip": str(address), "nonce": nonce}),
    }
