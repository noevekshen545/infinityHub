"""Loopback-воркер EmbeddingGemma 2.

Запускается только из app/modules/gemma_client.py в отдельном venv.
Никакого доступа из сети: bind 127.0.0.1 + токен в заголовке.
"""

import os
import threading

import numpy as np
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

MODEL = os.environ.get("GEMMA_MODEL", "google/embeddinggemma-2")
TOKEN = os.environ.get("GEMMA_TOKEN", "")
PORT = int(os.environ.get("GEMMA_PORT", "8299"))

app = FastAPI(title="embeddinggemma-worker")
_lock = threading.RLock()
_model = None
_device = "cpu"
_dtype = None
_dim = 768
_error = ""


def _load():
    global _model, _device, _dtype, _dim, _error
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        import torch
        from sentence_transformers import SentenceTransformer

        _device = "cuda" if torch.cuda.is_available() else "cpu"
        if _device == "cuda" and torch.cuda.is_bf16_supported():
            _dtype = torch.bfloat16
        else:
            _dtype = torch.float32
        kwargs = {"model_kwargs": {"torch_dtype": _dtype},
                  "device": _device}
        try:
            _model = SentenceTransformer(MODEL, config_kwargs={}, **kwargs)
        except TypeError:
            _model = SentenceTransformer(MODEL, **kwargs)
        try:
            _dim = int(_model.get_sentence_embedding_dimension() or 768)
        except Exception:
            _dim = 768
        _error = ""
        return _model


def _vecs(out):
    arr = np.asarray(out, dtype="float32")
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    arr = arr / norms
    return arr.tolist()


def _auth(token: str):
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


class TextIn(BaseModel):
    texts: list[str]
    prompt: str = "SearchQuery"


class ImagesIn(BaseModel):
    paths: list[str]


class AudioIn(BaseModel):
    path: str
    start: float = 0.0
    duration: float = 0.0


class VideoIn(BaseModel):
    paths: list[str]


@app.get("/health")
def health():
    return {"ok": True, "loaded": _model is not None, "device": _device,
            "dim": _dim, "model": MODEL, "error": _error}


@app.post("/warmup")
def warmup(x_token: str = Header(default="")):
    _auth(x_token)
    try:
        _load()
    except Exception as e:
        global _error
        _error = str(e)
        raise HTTPException(status_code=500, detail=_error)
    return {"ok": True, "device": _device, "dim": _dim}


@app.post("/embed/text")
def embed_text(body: TextIn, x_token: str = Header(default="")):
    _auth(x_token)
    model = _load()
    with _lock:
        try:
            out = model.encode(body.texts, prompt_name=body.prompt or None,
                               normalize_embeddings=True,
                               convert_to_numpy=True, batch_size=32)
        except Exception:
            out = model.encode(body.texts, normalize_embeddings=True,
                               convert_to_numpy=True, batch_size=32)
    return {"vectors": _vecs(out), "dim": _dim}


def _encode_dict(key: str, values, batch: int):
    """Вектор на каждый элемент. Один dict со списком значений модель считает
    ОДНИМ мультимодальным сэмплом и возвращает один вектор на всю пачку."""
    model = _load()
    with _lock:
        try:
            out = model.encode([{key: v} for v in values],
                               normalize_embeddings=True,
                               convert_to_numpy=True, batch_size=batch)
            arr = np.asarray(out)
            if arr.ndim == 2 and len(arr) == len(values):
                return _vecs(arr)
        except Exception:
            pass
        out = [model.encode({key: v}, normalize_embeddings=True,
                            convert_to_numpy=True) for v in values]
        return _vecs(np.vstack([np.asarray(x).reshape(1, -1) for x in out]))


@app.post("/embed/images")
def embed_images(body: ImagesIn, x_token: str = Header(default="")):
    _auth(x_token)
    if not body.paths:
        return {"vectors": [], "dim": _dim}
    return {"vectors": _encode_dict("image", body.paths, 8), "dim": _dim}


@app.post("/embed/video")
def embed_video(body: VideoIn, x_token: str = Header(default="")):
    _auth(x_token)
    if not body.paths:
        return {"vectors": [], "dim": _dim}
    return {"vectors": _encode_dict("video", body.paths, 2), "dim": _dim}


def _decode_audio(path: str, start: float, duration: float):
    import librosa
    kwargs = {"sr": 16000, "mono": True}
    if start:
        kwargs["offset"] = float(start)
    if duration:
        kwargs["duration"] = float(duration)
    y, _ = librosa.load(path, **kwargs)
    return y


@app.post("/embed/audio")
def embed_audio(body: AudioIn, x_token: str = Header(default="")):
    _auth(x_token)
    if not os.path.isfile(body.path):
        raise HTTPException(status_code=404, detail="audio not found")
    model = _load()
    try:
        y = _decode_audio(body.path, body.start, body.duration)
    except Exception as e:
        raise HTTPException(status_code=500, detail="decode: %s" % e)
    with _lock:
        out = model.encode({"audio": y}, normalize_embeddings=True,
                           convert_to_numpy=True)
    return {"vectors": _vecs(out), "dim": _dim}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
