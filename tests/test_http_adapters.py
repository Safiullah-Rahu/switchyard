"""Contract tests for the real-API adapters against mocked HTTP.

They pin down request shapes and, more importantly, how each failure is
classified: retry, stop, or "we don't know — reconcile first".
"""

import json

import httpx
import pytest

from switchyard.adapters.base import AmbiguousOutcome, PermanentError, Receipt, TransientError
from switchyard.adapters.hubspot import KEY_PROPERTY, HubSpotCrmAdapter
from switchyard.adapters.slack import SlackChatAdapter

STAGES = {"new": "appointmentscheduled", "proposal_sent": "presentationscheduled"}


def hubspot(handler):
    client = httpx.Client(base_url="https://api.hubapi.com", transport=httpx.MockTransport(handler))
    return HubSpotCrmAdapter("pat-test", stage_ids=STAGES, client=client)


def slack(handler):
    client = httpx.Client(base_url="https://slack.com/api", transport=httpx.MockTransport(handler))
    return SlackChatAdapter("xoxb-test", channel_ids={"#sales": "C0SALES"}, client=client)


DEAL = {"contact_id": "101", "title": "Northgate Bakery — solar", "stage": "new"}


def test_hubspot_deal_create_sends_key_and_association():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "9001"})

    receipt = hubspot(handler).execute("crm.create_deal", DEAL, "sy_key1")
    assert receipt.external_id == "9001"
    assert seen["auth"] == "Bearer pat-test"
    assert seen["body"]["properties"][KEY_PROPERTY] == "sy_key1"
    assert seen["body"]["properties"]["dealstage"] == "appointmentscheduled"
    assert seen["body"]["associations"][0]["to"]["id"] == "101"


def test_hubspot_conflict_on_unique_key_adopts_the_existing_deal():
    def handler(request):
        if request.url.path == "/crm/v3/objects/deals":
            return httpx.Response(409, json={"message": f"A deal with {KEY_PROPERTY}=sy_key1 already exists"})
        assert request.url.path == "/crm/v3/objects/deals/search"
        assert json.loads(request.content)["filterGroups"][0]["filters"][0]["value"] == "sy_key1"
        return httpx.Response(200, json={"results": [{"id": "9001"}]})

    receipt = hubspot(handler).execute("crm.create_deal", DEAL, "sy_key1")
    assert receipt.external_id == "9001" and receipt.details["adopted_on_conflict"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, headers={"Retry-After": "10"}), TransientError),
        (httpx.Response(502), TransientError),
        (httpx.Response(403, json={"message": "missing scope crm.objects.deals.write"}), PermanentError),
        (httpx.Response(400, json={"message": "Property values were not valid"}), PermanentError),
    ],
)
def test_hubspot_error_classification(response, error):
    with pytest.raises(error):
        hubspot(lambda request: response).execute("crm.create_deal", DEAL, "sy_key1")


def test_a_timeout_on_a_write_is_ambiguous_not_transient():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(AmbiguousOutcome):
        hubspot(handler).execute("crm.create_deal", DEAL, "sy_key1")


def test_a_refused_connection_is_safe_to_retry():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransientError):
        hubspot(handler).execute("crm.create_deal", DEAL, "sy_key1")


def test_hubspot_verify_reads_back_by_id():
    def handler(request):
        assert request.method == "GET" and request.url.path == "/crm/v3/objects/deals/9001"
        return httpx.Response(200, json={"id": "9001", "properties": {"dealname": "Something else"}})

    result = hubspot(handler).verify("crm.create_deal", Receipt("crm", "9001"), DEAL)
    assert not result.ok and "dealname" in result.mismatches[0]


def test_slack_post_carries_the_key_in_metadata():
    posted = {}

    def handler(request):
        if request.url.path.endswith("conversations.history"):
            return httpx.Response(200, json={"ok": True, "messages": []})
        posted.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "C0SALES", "ts": "1793000000.000100"})

    receipt = slack(handler).execute("chat.post", {"channel": "#sales", "text": "hi"}, "sy_key2")
    assert receipt.external_id == "1793000000.000100"
    assert posted["metadata"]["event_payload"]["idempotency_key"] == "sy_key2"


def test_slack_repost_finds_the_earlier_message_instead():
    def handler(request):
        assert request.url.path.endswith("conversations.history"), "must not post again"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {
                        "ts": "1.2",
                        "text": "hi",
                        "metadata": {
                            "event_type": "switchyard_action",
                            "event_payload": {"idempotency_key": "sy_key2"},
                        },
                    }
                ],
            },
        )

    receipt = slack(handler).execute("chat.post", {"channel": "#sales", "text": "hi"}, "sy_key2")
    assert receipt.external_id == "1.2" and receipt.details["found_by"] == "metadata"


@pytest.mark.parametrize(
    ("error", "kind"),
    [("ratelimited", TransientError), ("channel_not_found", PermanentError), ("missing_scope", PermanentError)],
)
def test_slack_ok_false_errors_are_classified(error, kind):
    def handler(request):
        if request.url.path.endswith("conversations.history"):
            return httpx.Response(200, json={"ok": True, "messages": []})
        return httpx.Response(200, json={"ok": False, "error": error})

    with pytest.raises(kind):
        slack(handler).execute("chat.post", {"channel": "#sales", "text": "hi"}, "k")
