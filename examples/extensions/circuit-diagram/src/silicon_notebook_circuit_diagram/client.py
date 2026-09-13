"""One OpenAI-compatible vision call, and the parsing of what comes back.

Like the arXiv sample's transport, this module knows the upstream vendor and
nothing else: no ``app.*`` import, and no dependency on the plugin's own
settings model either, so it stays replaceable on its own.  Callers map
deployment settings onto :func:`classify`'s keyword arguments (see
:func:`~silicon_notebook_circuit_diagram.settings.classify_kwargs`).

**The response is not trusted.**  A model asked for JSON returns JSON most of
the time; the rest of the time it returns JSON wrapped in a Markdown fence, or
prose, or an object missing half the keys it was asked for.  All four are
handled here — fenced output is unwrapped, missing keys fall back to their
defaults, and anything that is still not a JSON object raises — so the caller
above deals with exactly two outcomes: a classification, or one exception.

**No settings value ever reaches this module's exceptions.**  Every failure
below raises a fixed sentence, and none of them chains the originating
exception (``from None``): the endpoint, the model id and the credential are
all deployment configuration, and a chained ``URLError`` prints the URL it
failed on straight into whatever renders the traceback.  This plugin's
failures are counted by its caller and reported to core as a stable code
(SOP §3.6), so nothing downstream needs the original.

**Redirects are not followed.**  A 3xx is an error here, not a hop: the
credential travels in an ``Authorization`` header, and ``urllib``'s default
handler would replay that header at whatever host the response named. A
deployment that moved its endpoint changes ``base_url``.

**Registered limitation — ``timeout_seconds`` bounds one socket operation,
not the whole call.**  ``urllib.request.urlopen(..., timeout=...)`` resets
that clock on connect and on every partial ``read()``, so an upstream that
trickles bytes can outlast it.  What actually bounds this plugin's turn is
core's own ``SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS`` deadline, enforced by
the host on a thread it can abandon — the caller's per-image budget check
(:mod:`.enricher`) exists so the plugin stops on its own before that happens,
not because this module can guarantee it.

**Registered limitation — no outbound address policy.**  ``base_url`` is
deployment-configured rather than user input, so this module does not re-check
that the host resolves to a public address the way core's URL ingestion does.
A variant that let users choose the endpoint would have to add that check.
"""
from __future__ import annotations

import base64
import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

# The image media types DeepSeek's vision endpoint accepts.  Held here rather
# than in the settings model because it is a fact about the upstream, not a
# deployment choice: a variant pointed at a different provider replaces this
# module and this set with it.
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

# Plugin-private bounds; registered for operators in the package README.
MAX_RESPONSE_BYTES = 256 * 1024

Post = Callable[[str, bytes, dict, float], bytes]


class CircuitDiagramError(RuntimeError):
    """The one outcome a caller has to handle: this image was not classified.

    Deliberately a single class.  "The endpoint refused", "the response was
    not JSON" and "the response was JSON of the wrong shape" are the same
    decision for the caller — skip this image, count it, keep going — and a
    taxonomy nobody branches on is a taxonomy that drifts.
    """


@dataclass(frozen=True, slots=True)
class CircuitClassification:
    """What the model said about one image.

    ``netlist`` and ``function`` are whatever the model produced, normalized
    only for line endings.  Length ceilings and the persisted shape belong to
    the policy layer (:mod:`.enricher`), because they follow from core's
    budget rather than from the upstream.
    """

    is_circuit: bool
    netlist: str = ""
    function: str = ""


_PROMPTS = {
    "zh": (
        "你是电路图分析助手。判断这张图片是否是电路原理图。"
        "只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码围栏。"
        '格式：{"is_circuit": true 或 false, "netlist": "SPICE 风格网表",'
        ' "function": "这个电路做什么，用中文一两句话说明"}。'
        "如果不是电路原理图，is_circuit 为 false，另外两个字段给空字符串。"
    ),
    "en": (
        "You analyse schematics. Decide whether this image is an electronic "
        "circuit schematic. Output one JSON object and nothing else — no "
        "explanation, no Markdown code fence. Format: "
        '{"is_circuit": true or false, "netlist": "SPICE-style netlist", '
        '"function": "one or two sentences on what the circuit does"}. '
        "If it is not a schematic, set is_circuit to false and leave the "
        "other two fields as empty strings."
    ),
}


def prompt_text(language: str) -> str:
    """The instruction sent with the image.  Unknown languages fall back to zh."""

    return _PROMPTS.get(language, _PROMPTS["zh"])


def data_uri(image_bytes: bytes, mime: str) -> str:
    """The ``data:`` URI the ``image_url`` content part carries.

    The endpoint takes the image inline rather than by reference, which is
    what keeps this plugin from needing a publicly reachable asset URL — and
    therefore from needing core to expose one.
    """

    if mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise CircuitDiagramError("unsupported image media type")
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def request_body(
    *, model: str, image_bytes: bytes, mime: str, language: str
) -> dict:
    """Build the OpenAI-compatible chat/completions body.  Pure."""

    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text(language)},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_uri(image_bytes, mime)},
                    },
                ],
            }
        ],
        # Asked for, never relied on: `parse_classification` below still
        # handles a fenced or prose answer, because a deployment may point
        # `base_url` at a gateway that drops the field.
        "response_format": {"type": "json_object"},
    }


def classify(
    image_bytes: bytes,
    mime: str,
    *,
    url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    language: str = "zh",
    post: Post | None = None,
) -> CircuitClassification:
    """Classify one image, or raise :class:`CircuitDiagramError`.

    ``post`` is injectable so callers can be tested without a network — the
    same shape the arXiv sample's ``fetch`` seam uses.  It is resolved from
    this module at call time rather than bound as a default argument, so
    replacing the module attribute reaches every caller.
    """

    body = json.dumps(
        request_body(
            model=model, image_bytes=image_bytes, mime=mime, language=language
        )
    ).encode("utf-8")
    headers = {
        # Read by the caller from the environment at dial time and never
        # stored — see the settings module.
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    dial = post or _post
    try:
        payload = dial(url, body, headers, timeout_seconds)
    except CircuitDiagramError:
        raise
    except Exception:  # noqa: BLE001 — one stable outcome for the caller
        # ``from None``: a chained ``HTTPError``/``URLError`` carries the
        # endpoint in its own message, and this module's promise is that no
        # deployment configuration escapes through an exception.
        raise CircuitDiagramError("circuit classification request failed") from None
    return parse_classification(payload)


def parse_classification(payload: object) -> CircuitClassification:
    """Read one chat/completions response into a classification.

    Two layers of untrusted JSON, and they fail differently: the envelope is
    the endpoint's own contract, so a malformed one is an upstream fault,
    while ``message.content`` is model output, so a fenced or partial one is
    ordinary and handled rather than raised on.
    """

    if type(payload) is not bytes:
        raise CircuitDiagramError("circuit classification response was not bytes")
    try:
        envelope = json.loads(payload.decode("utf-8", "replace"))
        content = envelope["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001 — one stable outcome for the caller
        raise CircuitDiagramError(
            "circuit classification response envelope was malformed"
        ) from None
    if type(content) is not str:
        raise CircuitDiagramError(
            "circuit classification response carried no text content"
        )
    return classification_from_text(content)


def classification_from_text(text: str) -> CircuitClassification:
    """Parse the model's own answer: bare JSON, fenced JSON, or neither.

    Missing keys fall back to their defaults rather than raising: a model that
    answered ``{"is_circuit": false}`` and stopped has answered the question,
    and demanding the two fields it was told to leave empty would turn a
    correct negative into a counted failure.
    """

    data = _json_object(text)
    return CircuitClassification(
        is_circuit=_as_bool(data.get("is_circuit")),
        netlist=_as_text(data.get("netlist")),
        function=_as_text(data.get("function")),
    )


def _json_object(text: str) -> dict:
    """``text`` as a JSON object: bare, fenced, or embedded in prose.

    Three shapes, tried cheapest first.  The last one — everything from the
    first ``{`` to the last ``}`` — is what catches the model that answered
    "Sure! {...} Hope that helps." without a fence.  It is a scan, not a
    parser: whatever it slices out still has to survive ``json.loads``.
    """

    for candidate in (text, _unfenced(text), _braced(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:  # noqa: BLE001 — try the next shape, then give up
            continue
        if type(parsed) is dict:
            return parsed
    raise CircuitDiagramError("circuit classification answer was not a JSON object")


def _braced(text: str) -> str:
    """From the first ``{`` to the last ``}``, or ``""`` when there is neither."""

    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else ""


def _unfenced(text: str) -> str:
    """The body of the first ```-fenced block, or ``""`` when there is none.

    Line-based rather than a regular expression because the fenced content is
    itself arbitrary model output: scanning for the opening and closing fence
    lines cannot be made to backtrack, and an unclosed fence simply runs to
    the end of the text, which is the friendlier reading of a truncated
    answer.
    """

    lines = text.splitlines()
    opened = -1
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            opened = index
            break
    if opened < 0:
        return ""
    body: list[str] = []
    for line in lines[opened + 1 :]:
        if line.lstrip().startswith("```"):
            break
        body.append(line)
    return "\n".join(body)


def _as_bool(value: object) -> bool:
    """The three spellings of a boolean a JSON-shaped model actually emits.

    A real ``true``; the integer ``1`` (models trained on 0/1 labels answer
    that way); and the quoted string, in any case.  Everything else — a
    number that is not 1, ``null``, an absent key — is ``False``, which for
    this plugin means "no candidate", the safe direction.
    """

    if type(value) is bool:
        return value
    if type(value) is int:
        return value == 1
    if type(value) is str:
        return value.strip().lower() in {"true", "1"}
    return False


def _as_text(value: object) -> str:
    return value if type(value) is str else ""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every 3xx into an error instead of a second request.

    ``urllib``'s default handler replays the original request at the address
    the response names, ``Authorization`` header included — so a compromised
    or merely misconfigured endpoint could collect this deployment's API key
    by answering ``302``.  Returning ``None`` from ``redirect_request`` makes
    the 3xx surface as an ``HTTPError``, which the caller reports as one
    failed image.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# Built once at import: constructing an opener performs no I/O and opens no
# connection, so this is safe to do at module scope (and `configure` still
# builds nothing — see the SOP §3.2 rule the settings module cites).
_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _post(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
    """POST ``body`` and read at most :data:`MAX_RESPONSE_BYTES` of the reply.

    The ceiling bounds network cost and turns a response the endpoint never
    intended to send into an unparseable document, which the caller reports as
    a failed classification rather than an unbounded read.
    """

    request = urllib.request.Request(
        url, data=body, method="POST", headers=headers
    )
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES)
