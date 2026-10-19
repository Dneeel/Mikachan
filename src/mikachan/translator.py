"""On-demand translation via a public, unkeyed Google Translate endpoint.

The public endpoint has no uptime guarantee. Requests are deliberately bounded
and a provider failure is surfaced to the user rather than inventing a result.
"""

import httpx


async def translate_public(text: str, target: str) -> str:
    if target not in {"en", "fa"} or not 1 <= len(text) <= 2000:
        raise ValueError("Invalid translation request")
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
        )
        response.raise_for_status()
        if len(response.content) > 150_000:
            raise ValueError("Translation response is too large")
        data = response.json()
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise ValueError("Translation provider returned invalid data")
    parts = [segment[0] for segment in data[0]
             if isinstance(segment, list) and segment and isinstance(segment[0], str)]
    result = "".join(parts).strip()
    if not result or len(result) > 3500:
        raise ValueError("Translation provider returned no usable text")
    return result
