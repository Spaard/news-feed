import json

import httpx2
import pytest
from openai import AsyncOpenAI


class FakeFoundry:
    """Faux Foundry derrière le vrai SDK openai. Les tests définissent :
    - `embed(textes) -> vecteurs` ;
    - `chat(prompt_système, contenu_utilisateur) -> dict` : réponse JSON du modèle rapide
      (`None` : requête rejetée par le filtre de contenu) ;
    - `main(corps_de_la_requête) -> message` : {"content": ...} ou {"tool_calls": [...]} du modèle
      principal (sans contenu ni appel d'outil : réponse filtrée).
    `requests` garde le corps JSON de chaque requête reçue."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.embed = None
        self.chat = None
        self.main = None

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key="test",
            base_url="https://foundry.test/openai/v1/",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle)),
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if request.url.path.endswith("/embeddings"):
            data = [
                {"object": "embedding", "index": i, "embedding": vector}
                for i, vector in enumerate(self.embed(body["input"]))
            ]
            usage = {"prompt_tokens": 0, "total_tokens": 0}
            return httpx2.Response(
                200, json={"object": "list", "model": body["model"], "data": data, "usage": usage}
            )
        if body["model"] == "main":
            message = {"role": "assistant", "content": None, **self.main(body)}
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            finish = finish if message["content"] or message.get("tool_calls") else "content_filter"
        else:
            system, user = (message["content"] for message in body["messages"])
            reply = self.chat(system, json.loads(user))
            if reply is None:
                error = {"message": "filtered", "code": "content_filter", "param": "prompt"}
                return httpx2.Response(400, json={"error": error})
            message, finish = {"role": "assistant", "content": json.dumps(reply)}, "stop"
        return httpx2.Response(
            200,
            json={
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            },
        )


@pytest.fixture
def foundry() -> FakeFoundry:
    return FakeFoundry()
