"""Durable state. Missing/corrupt remote state is never treated as a fresh start."""

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import required


def empty_state() -> dict:
    return {
        "schema": 1,
        "scope": None,
        "cursor": 0,
        "initialized": False,
        "posts": {},
        "seen": [],
        "pending": None,
    }


def validate_state(state: dict) -> dict:
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise ValueError("Неподдерживаемый или повреждённый state.json")
    for key, expected in (("posts", dict), ("seen", list), ("cursor", int), ("initialized", bool)):
        if not isinstance(state.get(key), expected):
            raise ValueError(f"Повреждённое поле состояния: {key}")
    if "scope" not in state or "pending" not in state:
        raise ValueError("Неполный state.json")
    return state


class LocalStore:
    def __init__(self, path: str):
        self.path = Path(path)

    async def load(self) -> dict:
        if not self.path.exists():
            raise RuntimeError("Нет состояния. Сначала выполните schedule-bot init-state")
        return validate_state(json.loads(self.path.read_text(encoding="utf-8")))

    async def initialize(self):
        if self.path.exists():
            raise RuntimeError("Состояние уже существует; перезапись запрещена")
        await self.save(empty_state())

    async def ensure_initialized(self):
        if not self.path.exists():
            await self.initialize()

    async def save(self, state: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(validate_state(state), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)


class GitHubStore:
    FORMAT = "ak-schedule-state-aesgcm-v1"
    AAD = b"ak-schedule-bot:state:v1"

    def __init__(
        self,
        client: httpx.AsyncClient,
        repository: str,
        token: str,
        branch: str,
        encryption_secret: str,
    ):
        self.client, self.repository, self.branch = client, repository, branch
        if not encryption_secret:
            raise ValueError("STATE_ENCRYPTION_KEY нужен для шифрования состояния")
        self.cipher = AESGCM(hashlib.sha256(encryption_secret.encode()).digest())
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.sha = None
        self.state_hash = None

    async def request(self, method: str, path: str, **kwargs):
        # Do not include URLs, tokens, remote response bodies or HTTP exceptions in logs.
        try:
            response = await self.client.request(
                method,
                f"https://api.github.com/repos/{self.repository}/{path}",
                headers=self.headers,
                **kwargs,
            )
        except httpx.HTTPError:
            raise RuntimeError("GitHub API недоступен; операция остановлена") from None
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API: HTTP {response.status_code}; проверьте ветку и права")
        return response.json()

    async def initialize(self):
        repo = await self.request("GET", "")
        ref = await self.request("GET", f"git/ref/heads/{quote(repo['default_branch'], safe='')}")
        await self.request(
            "POST",
            "git/refs",
            json={"ref": f"refs/heads/{self.branch}", "sha": ref["object"]["sha"]},
        )
        await self.save(empty_state())

    async def ensure_initialized(self):
        # Only a missing branch is a new installation. Missing/corrupt state in an
        # existing branch must never reset the delivery history.
        try:
            response = await self.client.get(
                f"https://api.github.com/repos/{self.repository}/git/ref/heads/"
                + quote(self.branch, safe=""),
                headers=self.headers,
            )
        except httpx.HTTPError:
            raise RuntimeError("GitHub API недоступен; операция остановлена") from None
        if response.status_code == 404:
            await self.initialize()
        elif response.status_code != 200:
            raise RuntimeError(f"GitHub API: HTTP {response.status_code}; проверьте права")

    async def load(self) -> dict:
        data = await self.request("GET", "contents/state.json", params={"ref": self.branch})
        self.sha = data["sha"]
        if data.get("encoding") != "base64":
            data = await self.request("GET", f"git/blobs/{self.sha}")
        envelope = json.loads(base64.b64decode(data["content"]))
        if envelope.get("format") != self.FORMAT:
            raise ValueError("Состояние не зашифровано или имеет неизвестный формат")
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        try:
            plaintext = self.cipher.decrypt(nonce, ciphertext, self.AAD)
        except InvalidTag:
            raise ValueError(
                "Не удалось расшифровать состояние; проверьте STATE_ENCRYPTION_KEY"
            ) from None
        state = validate_state(json.loads(plaintext))
        self.state_hash = hashlib.sha256(plaintext).hexdigest()
        return state

    async def save(self, state: dict):
        plaintext = json.dumps(
            validate_state(state), ensure_ascii=False, separators=(",", ":")
        ).encode()
        state_hash = hashlib.sha256(plaintext).hexdigest()
        if self.state_hash == state_hash:
            return
        nonce = secrets.token_bytes(12)
        ciphertext = self.cipher.encrypt(nonce, plaintext, self.AAD)
        envelope = json.dumps(
            {
                "format": self.FORMAT,
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode(),
            },
            separators=(",", ":"),
        )
        payload = {
            "message": "Update schedule bot state [skip ci]",
            "branch": self.branch,
            "content": base64.b64encode(envelope.encode()).decode(),
        }
        if self.sha:
            payload["sha"] = self.sha
        result = await self.request("PUT", "contents/state.json", json=payload)
        self.sha = result["content"]["sha"]
        self.state_hash = state_hash


def make_store(client: httpx.AsyncClient):
    backend = os.getenv("STATE_BACKEND", "local")
    if backend == "local":
        return LocalStore(os.getenv("STATE_FILE", "state/state.json"))
    if backend == "github":
        return GitHubStore(
            client,
            required("GITHUB_REPOSITORY"),
            required("GITHUB_TOKEN"),
            os.getenv("STATE_BRANCH", "bot-state"),
            required("STATE_ENCRYPTION_KEY"),
        )
    raise ValueError("STATE_BACKEND: ожидается local или github")
