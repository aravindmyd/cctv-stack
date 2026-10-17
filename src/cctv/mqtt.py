"""The paho-mqtt adapter — the only part of the pipeline that needs a broker.

Deliberately thin. *What* a review means lives in [`cctv.reviews`][cctv.reviews] and *when*
it gets worked on lives in [`cctv.dispatch`][cctv.dispatch], so this module only has to do
one thing: turn a socket message into a call to
[`ReviewDispatcher.dispatch`][cctv.dispatch.ReviewDispatcher.dispatch]. It is also the one
layer that cannot be unit-tested without a broker, which is a good reason to keep it small
enough to read in one sitting.

Two decisions worth naming:

**`paho` is imported inside [`start`][cctv.mqtt.ReviewSubscriber.start], not at module
import.** A deployment that runs sweep-only — no broker at all — must not need the
dependency installed. Importing at the top would make an optional component mandatory,
which is the kind of coupling that is invisible until someone builds a minimal image.

**`connect_async`, not `connect`.** `docker compose up` starts containers in whatever order
it likes, so the broker is frequently not listening yet. A synchronous connect would make
that a crash loop that resolves itself only because of `restart: unless-stopped`, and a
service that depends on being restarted to become healthy is a service whose first failure
looks like a real one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .dispatch import ReviewDispatcher

__all__ = ["MqttUnavailable", "ReviewSubscriber"]

logger = logging.getLogger(__name__)


class MqttUnavailable(RuntimeError):
    """`paho-mqtt` is not installed, so no subscription can be made."""


@dataclass
class ReviewSubscriber:
    """Subscribes to `frigate/reviews` and hands every message to the dispatcher."""

    dispatcher: ReviewDispatcher
    host: str = "mosquitto"
    port: int = 1883
    topic_prefix: str = "frigate"
    client_id: str = "cctv-clip-worker"
    keepalive_s: int = 60
    username: str | None = None
    password: str | None = None
    #: Counted rather than only logged. A message that is not JSON is a message nothing
    #: downstream will ever see, and a discard that exists only as a log line is a discard
    #: nobody can alert on.
    malformed: int = field(default=0, init=False)
    _client: Any = field(default=None, init=False, repr=False)

    @property
    def topic(self) -> str:
        return f"{self.topic_prefix}/reviews"

    # -----------------------------------------------------------------------------------

    def start(self) -> None:
        """Connect and begin consuming. Returns immediately; paho runs its own thread."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise MqttUnavailable(
                "paho-mqtt is not installed, so no broker subscription is possible. "
                "Install the `mqtt` extra, or run sweep-only by leaving CCTV_MQTT_HOST "
                "unset."
            ) from exc

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        if self.username is not None:
            client.username_pw_set(self.username, self.password)

        # Bounded backoff: a broker that is down should not be hammered, and one that comes
        # back should be picked up within seconds rather than minutes.
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        client.connect_async(self.host, self.port, keepalive=self.keepalive_s)
        client.loop_start()
        self._client = client

        logger.info("mqtt subscribing", extra={"topic": self.topic, "host": self.host})

    def stop(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            # Shutdown must not raise: this runs in a `finally`, and an exception here
            # would replace whatever real error was already propagating.
            logger.debug("mqtt shutdown raised; ignoring", exc_info=True)

    # -----------------------------------------------------------------------------------
    # Callbacks. These run on paho's network thread: they must not block and must not raise.
    # -----------------------------------------------------------------------------------

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        # `is_failure` exists on paho 2.x reason codes; the `!= 0` fallback keeps this
        # working if a plain int arrives instead.
        failed = getattr(reason_code, "is_failure", reason_code != 0)
        if failed:
            logger.error("mqtt connection refused", extra={"reason": str(reason_code)})
            return

        # Subscribing here, not in `start()`, is what makes a *re*connection work. paho
        # re-establishes the transport by itself, but subscriptions live on the session,
        # which a reconnect does not restore unless the broker was told to keep it.
        client.subscribe(self.topic, qos=0)
        logger.info("mqtt connected", extra={"topic": self.topic, "host": self.host})

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        # Not an error: paho reconnects on its own. Logged because a broker that flaps is
        # a real problem, and the only evidence of it is this line repeating.
        logger.warning("mqtt disconnected", extra={"reason": str(reason_code)})

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            raw = message.payload.decode("utf-8", "replace")
        except Exception:
            # An exception escaping a paho callback is swallowed by the network loop, so
            # this is not defensiveness for its own sake — it is the only place the failure
            # can be observed at all.
            self.malformed += 1
            logger.warning("mqtt message could not be decoded", extra={"topic": self.topic})
            return

        try:
            payload = json.loads(raw)
        except ValueError:
            self.malformed += 1
            logger.warning("mqtt message was not JSON", extra={"topic": self.topic})
            return

        if not isinstance(payload, dict):
            self.malformed += 1
            logger.warning("mqtt message was not an object", extra={"topic": self.topic})
            return

        # `dispatch` never raises and never blocks: it parses, enqueues, and returns. Doing
        # the work here would stall the network loop for the tens of seconds a fetch takes,
        # and the messages lost during that window are lost silently.
        self.dispatcher.dispatch(payload)
