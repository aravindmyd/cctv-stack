"""Tests for the MQTT adapter — the one layer that needs a broker in production.

Two claims here are invisible when they break:

**`paho` must stay optional.** A sweep-only deployment polls Frigate's API and has no broker
at all, so importing `cctv.mqtt` must not import `paho`. A top-level import would make an
optional dependency mandatory, and the only symptom would be a minimal image failing to
start — which is discovered by building that image, not by running this suite.

**A callback must not raise and must not be silent.** paho swallows an exception raised
inside a callback, so one malformed message would stop every future message from being seen
with no error anywhere. Malformed input is therefore *counted* and logged rather than
raised — and, crucially, the next good message still arrives.

A fake `paho` module is injected rather than the real client, because the behaviour worth
pinning is *which* paho calls are made (`connect_async`, and a subscription on every
connect). A test against a real broker could not assert either one.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from cctv.mqtt import MqttUnavailable, ReviewSubscriber

TOPIC = "frigate/reviews"


class Recorder:
    """Stands in for the dispatcher. Records payloads instead of queueing them."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def dispatch(self, review: dict[str, Any]) -> None:
        self.payloads.append(review)


class Message:
    """The part of a paho message this module touches."""

    def __init__(self, payload: object, topic: str = TOPIC) -> None:
        self.payload = payload
        self.topic = topic


def subscriber(**kwargs: Any) -> tuple[ReviewSubscriber, Recorder]:
    dispatcher = Recorder()
    return ReviewSubscriber(dispatcher=dispatcher, **kwargs), dispatcher


def payload(**overrides: Any) -> bytes:
    review = {
        "id": "1718987129.308396-fqk5ka",
        "camera": "front-door",
        "severity": "alert",
        "start_time": 1_700_000_000.0,
        "end_time": 1_700_000_030.0,
        "data": {"detections": ["ev-1"], "objects": ["person"]},
    }
    review.update(overrides)
    return json.dumps(review).encode()


# --------------------------------------------------------------------------------------
# The topic
# --------------------------------------------------------------------------------------


def test_the_default_topic_is_frigates_review_topic() -> None:
    subject, _ = subscriber()

    assert subject.topic == TOPIC


def test_the_topic_prefix_is_configurable() -> None:
    subject, _ = subscriber(topic_prefix="home")

    assert subject.topic == "home/reviews"


# --------------------------------------------------------------------------------------
# The callback — parse, count, hand over
# --------------------------------------------------------------------------------------


def test_a_review_message_reaches_the_dispatcher() -> None:
    subject, dispatcher = subscriber()

    subject._on_message(None, None, Message(payload()))

    assert dispatcher.payloads == [
        {
            "id": "1718987129.308396-fqk5ka",
            "camera": "front-door",
            "severity": "alert",
            "start_time": 1_700_000_000.0,
            "end_time": 1_700_000_030.0,
            "data": {"detections": ["ev-1"], "objects": ["person"]},
        }
    ]
    assert subject.malformed == 0


def test_a_message_that_is_not_json_is_counted_and_does_not_raise(logs) -> None:
    subject, dispatcher = subscriber()

    subject._on_message(None, None, Message(b"{not json"))

    assert subject.malformed == 1
    assert dispatcher.payloads == []
    assert logs.last()["msg"] == "mqtt message was not JSON"


@pytest.mark.parametrize(
    "raw",
    [
        b"[1, 2, 3]",
        b'"a string"',
        b"42",
        b"true",
        b"null",
    ],
)
def test_json_that_is_not_an_object_is_counted_rather_than_dispatched(
    raw: bytes,
) -> None:
    """JSON that parses to a list, a number or a string would break `dispatch`'s contract
    that it is handed a mapping, and the exception would be swallowed by the network loop.
    """
    subject, dispatcher = subscriber()

    subject._on_message(None, None, Message(raw))

    assert dispatcher.payloads == []
    assert subject.malformed == 1


def test_an_empty_object_is_handed_over_for_the_dispatcher_to_refuse() -> None:
    """The boundary between the two layers: this one knows about JSON, `dispatch` knows
    about reviews. An empty object is well-formed JSON, so it is not this layer's call.
    """
    subject, dispatcher = subscriber()

    subject._on_message(None, None, Message(b"{}"))

    assert dispatcher.payloads == [{}]
    assert subject.malformed == 0


def test_a_payload_that_cannot_even_be_decoded_is_counted() -> None:
    """paho always hands over bytes, so this branch is reached only by a caller that hands
    over something else — and it exists because an exception escaping a callback is
    swallowed by the network loop, leaving no trace at all."""
    subject, dispatcher = subscriber()

    subject._on_message(None, None, Message(object()))

    assert subject.malformed == 1
    assert dispatcher.payloads == []


def test_undecodable_bytes_land_in_the_json_path_not_the_decode_path(logs) -> None:
    """`decode(..., "replace")` means invalid UTF-8 never reaches the first `except`. Worth
    pinning, because a reader would reasonably assume it does, and the two branches log
    different things."""
    subject, _ = subscriber()

    subject._on_message(None, None, Message(b"\xff\xfe\xfa"))

    assert subject.malformed == 1
    assert logs.last()["msg"] == "mqtt message was not JSON"


def test_undecodable_bytes_do_not_raise() -> None:
    subject, _ = subscriber()

    subject._on_message(None, None, Message(b"\xff\xfe\xfa"))  # must not raise


def test_one_bad_message_does_not_stop_the_next_one() -> None:
    """The failure this prevents is total and silent: the broker keeps delivering, nothing
    is processed, and the only evidence is that clips stop appearing."""
    subject, dispatcher = subscriber()

    subject._on_message(None, None, Message(b"nonsense"))
    subject._on_message(None, None, Message(payload()))
    subject._on_message(None, None, Message(b"more nonsense"))
    subject._on_message(None, None, Message(payload(id="r-2")))

    assert subject.malformed == 2
    assert [p["id"] for p in dispatcher.payloads] == ["1718987129.308396-fqk5ka", "r-2"]


# --------------------------------------------------------------------------------------
# The callbacks paho calls on its own thread
# --------------------------------------------------------------------------------------


def test_a_successful_connection_subscribes_to_the_topic() -> None:
    subject, _ = subscriber()
    client = FakeClient()

    subject._on_connect(client, None, None, SimpleNamespace(is_failure=False))

    assert client.subscriptions == [(TOPIC, 0)]


def test_every_reconnection_subscribes_again() -> None:
    """paho restores the transport by itself, but a subscription lives on the session, so a
    reconnect that does not re-subscribe receives nothing — silently, forever."""
    subject, _ = subscriber()
    client = FakeClient()

    subject._on_connect(client, None, None, SimpleNamespace(is_failure=False))
    subject._on_disconnect(client, None, None, SimpleNamespace())
    subject._on_connect(client, None, None, SimpleNamespace(is_failure=False))

    assert client.subscriptions == [(TOPIC, 0), (TOPIC, 0)]


def test_a_refused_connection_is_logged_and_does_not_subscribe(logs) -> None:
    subject, _ = subscriber()
    client = FakeClient()

    subject._on_connect(client, None, None, SimpleNamespace(is_failure=True))

    assert client.subscriptions == []
    assert logs.last()["msg"] == "mqtt connection refused"


@pytest.mark.parametrize("reason_code", [0, 1, 135])
def test_a_plain_integer_reason_code_is_understood(reason_code: int) -> None:
    """paho 2.x hands over a reason-code object with `is_failure`; the `!= 0` fallback keeps
    this working if a plain int arrives instead, and without it a refusal would look like a
    success and never be logged."""
    subject, _ = subscriber()
    client = FakeClient()

    subject._on_connect(client, None, None, reason_code)

    assert client.subscriptions == ([] if reason_code else [(TOPIC, 0)])


def test_a_disconnect_is_logged_as_a_warning_not_an_error(logs) -> None:
    """Not an error — paho reconnects on its own. Logged because a broker that flaps is a
    real problem and this repeating line is the only evidence of it."""
    subject, _ = subscriber()

    subject._on_disconnect(FakeClient(), None, None, SimpleNamespace())

    assert logs.last()["level"] == "WARNING"
    assert logs.last()["msg"] == "mqtt disconnected"


# --------------------------------------------------------------------------------------
# start / stop, against a fake paho
# --------------------------------------------------------------------------------------


class FakeClient:
    """The paho client surface `cctv.mqtt` is allowed to touch.

    `connect` deliberately does not exist: if `start()` ever used it, this class would raise
    instead of the test quietly passing, because a synchronous connect turns "the broker is
    not up yet" into a crash loop that only `restart: unless-stopped` hides.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.connected_to: tuple[str, int, int] | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.disconnect_error: Exception | None = None
        self.subscriptions: list[tuple[str, int]] = []
        self.subscribe_failures: list[tuple[str, int]] = []
        self.reconnect_delays: tuple[int, int] | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.on_connect: Any = None
        self.on_message: Any = None
        self.on_disconnect: Any = None

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password)

    def reconnect_delay_set(self, *, min_delay: int, max_delay: int) -> None:
        self.reconnect_delays = (min_delay, max_delay)

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append((topic, qos))

    def disconnect(self) -> None:
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.disconnected = True


@pytest.fixture
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a fake `paho.mqtt.client`, so the calls `start()` makes are observable."""
    clients: list[FakeClient] = []

    def factory(*args: Any, **kwargs: Any) -> FakeClient:
        client = FakeClient(*args, **kwargs)
        clients.append(client)
        return client

    client_module = types.ModuleType("paho.mqtt.client")
    client_module.Client = factory
    client_module.CallbackAPIVersion = SimpleNamespace(VERSION2=2)

    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.__path__ = []  # type: ignore[attr-defined]
    mqtt_module.client = client_module

    paho_module = types.ModuleType("paho")
    paho_module.__path__ = []  # type: ignore[attr-defined]
    paho_module.mqtt = mqtt_module

    for name, module in [
        ("paho", paho_module),
        ("paho.mqtt", mqtt_module),
        ("paho.mqtt.client", client_module),
    ]:
        monkeypatch.setitem(sys.modules, name, module)

    return SimpleNamespace(clients=clients, factory=factory, module=client_module)


def test_start_connects_asynchronously(fake_paho: SimpleNamespace) -> None:
    subject, _ = subscriber(host="mosquitto", port=1883, keepalive_s=60)

    subject.start()

    client = fake_paho.clients[0]
    assert client.connected_to == ("mosquitto", 1883, 60)
    assert client.loop_started


def test_start_uses_the_paho_2_callback_api(fake_paho: SimpleNamespace) -> None:
    """The callbacks here take paho 2.x's five-argument shape, including `properties`. A
    client built for the 1.x API would call them with four and the extra argument would
    never be supplied — which only shows up on the first message."""
    subject, _ = subscriber(client_id="cctv-clip-worker")

    subject.start()

    client = fake_paho.clients[0]
    assert client.args == (fake_paho.module.CallbackAPIVersion.VERSION2,)
    assert client.kwargs["client_id"] == "cctv-clip-worker"


def test_start_registers_the_callbacks(fake_paho: SimpleNamespace) -> None:
    subject, _ = subscriber()

    subject.start()

    client = fake_paho.clients[0]
    assert client.on_connect == subject._on_connect
    assert client.on_message == subject._on_message
    assert client.on_disconnect == subject._on_disconnect


def test_start_sets_a_bounded_reconnect_backoff(fake_paho: SimpleNamespace) -> None:
    """A broker that is down should not be hammered, and one that comes back should be
    picked up within seconds."""
    subject, _ = subscriber()

    subject.start()

    assert fake_paho.clients[0].reconnect_delays == (1, 30)


def test_credentials_are_sent_when_a_username_is_configured(
    fake_paho: SimpleNamespace,
) -> None:
    subject, _ = subscriber(username="cctv", password="hunter2")

    subject.start()

    assert fake_paho.clients[0].credentials == ("cctv", "hunter2")


def test_no_credentials_are_sent_when_no_username_is_configured(
    fake_paho: SimpleNamespace,
) -> None:
    """A broker on the compose network needs no authentication, and a spurious CONNECT with
    an empty username is rejected by the ones that do."""
    subject, _ = subscriber()

    subject.start()

    assert fake_paho.clients[0].credentials is None


def test_start_keeps_the_client_for_stop(fake_paho: SimpleNamespace) -> None:
    subject, _ = subscriber()

    subject.start()

    assert subject._client is fake_paho.clients[0]


def test_stop_disconnects_and_stops_the_network_loop(
    fake_paho: SimpleNamespace,
) -> None:
    subject, _ = subscriber()
    subject.start()
    client = fake_paho.clients[0]

    subject.stop()

    assert client.disconnected
    assert client.loop_stopped
    assert subject._client is None


def test_stop_before_start_is_harmless() -> None:
    """`main` calls `stop` from a `finally`, which runs on paths where `start` never did."""
    subject, _ = subscriber()

    subject.stop()  # must not raise


def test_stop_swallows_a_failing_disconnect(fake_paho: SimpleNamespace) -> None:
    """This runs in a `finally`, so an exception here would replace whatever real error was
    already propagating — converting a diagnosis into a second, louder mystery."""
    subject, _ = subscriber()
    subject.start()
    fake_paho.clients[0].disconnect_error = RuntimeError("the socket was already gone")

    subject.stop()  # must not raise

    assert subject._client is None  # and a second stop does not retry it


# --------------------------------------------------------------------------------------
# paho is an optional dependency
# --------------------------------------------------------------------------------------


class NoPaho:
    """An import hook that makes `import paho` fail, exactly as an image built without the
    `mqtt` extra would."""

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "paho" or fullname.startswith("paho."):
            raise ImportError(f"No module named {fullname!r}")
        return None


@pytest.fixture
def without_paho(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Re-import `cctv.mqtt` with `paho` unimportable."""
    for name in [n for n in sys.modules if n == "paho" or n.startswith("paho.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "cctv.mqtt", raising=False)
    monkeypatch.setattr(sys, "meta_path", [NoPaho(), *sys.meta_path])

    return importlib.import_module("cctv.mqtt")


def test_the_blocker_really_blocks(without_paho: Any) -> None:
    """Guards the two tests below: if the hook did nothing, they would pass for the wrong
    reason."""
    with pytest.raises(ImportError):
        importlib.import_module("paho")


def test_the_module_imports_without_paho(without_paho: Any) -> None:
    """A sweep-only deployment has no broker, so it must not need the client installed."""
    assert without_paho.ReviewSubscriber is not None


def test_starting_without_paho_raises_an_actionable_error(without_paho: Any) -> None:
    """Both ways out are named: install the extra, or leave `CCTV_MQTT_HOST` unset and run
    sweep-only."""
    subject = without_paho.ReviewSubscriber(dispatcher=Recorder())

    with pytest.raises(without_paho.MqttUnavailable) as excinfo:
        subject.start()

    message = str(excinfo.value)
    assert "mqtt" in message
    assert "CCTV_MQTT_HOST" in message


def test_the_unavailable_error_is_a_runtime_error() -> None:
    """So a caller can catch `RuntimeError` generically if it must, and so `main`'s
    `except MqttUnavailable` cannot be defeated by an ordering mistake in the class
    hierarchy."""
    assert issubclass(MqttUnavailable, RuntimeError)


def test_importing_the_adapter_does_not_import_paho(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stated as `sys.modules`, because that is the observable form of the claim."""
    for name in [n for n in sys.modules if n == "paho" or n.startswith("paho.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "cctv.mqtt", raising=False)

    importlib.import_module("cctv.mqtt")

    assert "paho" not in sys.modules
